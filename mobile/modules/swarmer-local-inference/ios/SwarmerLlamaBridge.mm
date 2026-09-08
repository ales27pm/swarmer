#import "SwarmerLlamaBridge.h"

#import <llama/llama.h>

#include <algorithm>
#include <atomic>
#include <climits>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace {

NSString *const SwarmerLlamaErrorDomain = @"org.27pm.swarmer.llama";

NSError *LlamaError(NSInteger code, NSString *message) {
  return [NSError errorWithDomain:SwarmerLlamaErrorDomain
                             code:code
                         userInfo:@{NSLocalizedDescriptionKey: message}];
}

void InitializeBackendOnce() {
  static std::once_flag once;
  std::call_once(once, [] { llama_backend_init(); });
}

struct LlamaState {
  llama_model *model = nullptr;
  llama_context *context = nullptr;
  std::atomic_bool cancelRequested{false};
  std::atomic_bool loaded{false};
  std::atomic_bool busy{false};

  ~LlamaState() { unload(); }

  void unload() {
    if (context != nullptr) {
      llama_free(context);
      context = nullptr;
    }
    if (model != nullptr) {
      llama_model_free(model);
      model = nullptr;
    }
    loaded.store(false, std::memory_order_release);
  }
};

bool ContinueModelLoad(float, void *userData) {
  LlamaState *state = static_cast<LlamaState *>(userData);
  return state != nullptr && !state->cancelRequested.load(std::memory_order_acquire);
}

struct BatchOwner {
  llama_batch value;

  explicit BatchOwner(int32_t capacity) : value(llama_batch_init(capacity, 0, 1)) {}
  ~BatchOwner() { llama_batch_free(value); }

  BatchOwner(const BatchOwner &) = delete;
  BatchOwner &operator=(const BatchOwner &) = delete;
};

struct SamplerDeleter {
  void operator()(llama_sampler *sampler) const {
    if (sampler != nullptr) { llama_sampler_free(sampler); }
  }
};

using SamplerOwner = std::unique_ptr<llama_sampler, SamplerDeleter>;

bool AddSampler(llama_sampler *chain, llama_sampler *component, NSError **error) {
  if (component == nullptr) {
    if (error != nullptr) { *error = LlamaError(27, @"llama.cpp could not allocate a sampler component."); }
    return false;
  }
  llama_sampler_chain_add(chain, component);
  return true;
}

bool Tokenize(const llama_vocab *vocab, NSString *prompt, std::vector<llama_token> &tokens,
              NSError **error) {
  NSData *utf8 = [prompt dataUsingEncoding:NSUTF8StringEncoding];
  if (utf8.length == 0 || utf8.length > INT32_MAX) {
    if (error != nullptr) { *error = LlamaError(20, @"The prompt is empty or too large."); }
    return false;
  }

  const char *bytes = static_cast<const char *>(utf8.bytes);
  const int32_t byteCount = static_cast<int32_t>(utf8.length);
  int32_t capacity = std::max<int32_t>(32, byteCount + 8);
  tokens.resize(static_cast<size_t>(capacity));

  int32_t count = llama_tokenize(vocab, bytes, byteCount, tokens.data(), capacity, true, false);
  if (count == INT32_MIN) {
    if (error != nullptr) { *error = LlamaError(21, @"Tokenization overflowed llama.cpp limits."); }
    return false;
  }
  if (count < 0) {
    capacity = -count;
    tokens.resize(static_cast<size_t>(capacity));
    count = llama_tokenize(vocab, bytes, byteCount, tokens.data(), capacity, true, false);
  }
  if (count <= 0) {
    if (error != nullptr) { *error = LlamaError(22, @"The GGUF tokenizer rejected the prompt."); }
    return false;
  }
  tokens.resize(static_cast<size_t>(count));
  return true;
}

bool AppendTokenPiece(const llama_vocab *vocab, llama_token token, std::vector<uint8_t> &output,
                      NSError **error) {
  std::vector<char> piece(128);
  int32_t count = llama_token_to_piece(vocab, token, piece.data(), static_cast<int32_t>(piece.size()),
                                       0, false);
  if (count < 0) {
    piece.resize(static_cast<size_t>(-count));
    count = llama_token_to_piece(vocab, token, piece.data(), static_cast<int32_t>(piece.size()),
                                 0, false);
  }
  if (count < 0) {
    if (error != nullptr) { *error = LlamaError(23, @"llama.cpp could not decode an output token."); }
    return false;
  }
  output.insert(output.end(), piece.begin(), piece.begin() + count);
  return true;
}

void FillBatch(llama_batch &batch, const llama_token *tokens, int32_t count, int32_t startPosition,
               bool requestLastLogits) {
  batch.n_tokens = count;
  for (int32_t index = 0; index < count; ++index) {
    batch.token[index] = tokens[index];
    batch.pos[index] = startPosition + index;
    batch.n_seq_id[index] = 1;
    batch.seq_id[index][0] = 0;
    batch.logits[index] = requestLastLogits && index == count - 1 ? 1 : 0;
  }
}

}  // namespace

@interface SwarmerLlamaBridge ()
@property(nonatomic, assign) void *statePointer;
@property(nonatomic, strong) dispatch_queue_t inferenceQueue;
@end

@implementation SwarmerLlamaBridge

- (instancetype)init {
  self = [super init];
  if (self != nil) {
    InitializeBackendOnce();
    _statePointer = new LlamaState();
    _inferenceQueue = dispatch_queue_create("org.27pm.swarmer.llama.inference", DISPATCH_QUEUE_SERIAL);
  }
  return self;
}

- (void)dealloc {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  if (state != nullptr) {
    dispatch_sync(_inferenceQueue, ^{ state->unload(); });
    delete state;
    _statePointer = nullptr;
  }
}

- (BOOL)isLoaded {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  return state != nullptr && state->loaded.load(std::memory_order_acquire);
}

- (BOOL)isBusy {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  return state != nullptr && state->busy.load(std::memory_order_acquire);
}

- (BOOL)loadModelAtPath:(NSString *)path
            contextSize:(NSInteger)contextSize
                  error:(NSError **)error {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  if (state == nullptr) {
    if (error != nullptr) { *error = LlamaError(1, @"The llama.cpp runtime is unavailable."); }
    return NO;
  }
  if (state->busy.load(std::memory_order_acquire)) {
    if (error != nullptr) { *error = LlamaError(2, @"Generation is already in progress."); }
    return NO;
  }

  __block BOOL succeeded = NO;
  __block NSError *localError = nil;
  dispatch_sync(_inferenceQueue, ^{
    state->unload();

    if (![[path.pathExtension lowercaseString] isEqualToString:@"gguf"]) {
      localError = LlamaError(3, @"llama.cpp accepts only a GGUF model file.");
      return;
    }

    llama_model_params modelParams = llama_model_default_params();
    modelParams.n_gpu_layers = -1;
    modelParams.check_tensors = true;
    modelParams.progress_callback = ContinueModelLoad;
    modelParams.progress_callback_user_data = state;
    state->model = llama_model_load_from_file(path.fileSystemRepresentation, modelParams);
    if (state->model == nullptr) {
      localError = LlamaError(4, @"llama.cpp could not load the GGUF model.");
      return;
    }
    if (llama_model_has_encoder(state->model) || !llama_model_has_decoder(state->model)) {
      state->unload();
      localError = LlamaError(5, @"Only decoder-only GGUF language models are supported.");
      return;
    }

    const uint32_t boundedContext = static_cast<uint32_t>(std::clamp<NSInteger>(contextSize, 256, 8192));
    llama_context_params contextParams = llama_context_default_params();
    contextParams.n_ctx = boundedContext;
    contextParams.n_batch = std::min<uint32_t>(boundedContext, 512);
    contextParams.n_ubatch = std::min<uint32_t>(contextParams.n_batch, 128);
    contextParams.n_seq_max = 1;
    contextParams.n_threads = std::max<int32_t>(1, static_cast<int32_t>(NSProcessInfo.processInfo.activeProcessorCount / 2));
    contextParams.n_threads_batch = std::max<int32_t>(1, static_cast<int32_t>(NSProcessInfo.processInfo.activeProcessorCount));
    contextParams.offload_kqv = true;

    state->context = llama_init_from_model(state->model, contextParams);
    if (state->context == nullptr) {
      state->unload();
      localError = LlamaError(6, @"llama.cpp could not allocate a bounded inference context.");
      return;
    }

    state->loaded.store(true, std::memory_order_release);
    succeeded = YES;
  });

  if (!succeeded && error != nullptr) { *error = localError; }
  return succeeded;
}

- (NSString *)generatePrompt:(NSString *)prompt
                     maxTokens:(NSInteger)maxTokens
                   temperature:(double)temperature
                         error:(NSError **)error {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  if (state == nullptr || !state->loaded.load(std::memory_order_acquire)) {
    if (error != nullptr) { *error = LlamaError(10, @"No GGUF model is loaded."); }
    return nil;
  }

  bool expected = false;
  if (!state->busy.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
    if (error != nullptr) { *error = LlamaError(11, @"Generation is already in progress."); }
    return nil;
  }

  __block NSDictionary<NSString *, id> *result = nil;
  __block NSError *localError = nil;
  dispatch_sync(_inferenceQueue, ^{
    struct BusyReset {
      LlamaState *state;
      ~BusyReset() { state->busy.store(false, std::memory_order_release); }
    } reset{state};

    llama_context *context = state->context;
    llama_model *model = state->model;
    if (context == nullptr || model == nullptr) {
      localError = LlamaError(12, @"The GGUF model was unloaded before generation.");
      return;
    }

    const llama_vocab *vocab = llama_model_get_vocab(model);
    if (vocab == nullptr) {
      localError = LlamaError(13, @"The GGUF model has no tokenizer vocabulary.");
      return;
    }

    std::vector<llama_token> promptTokens;
    if (!Tokenize(vocab, prompt, promptTokens, &localError)) { return; }

    const int32_t boundedMaxTokens = static_cast<int32_t>(std::clamp<NSInteger>(maxTokens, 1, 512));
    const uint32_t contextCapacity = llama_n_ctx(context);
    if (promptTokens.size() + static_cast<size_t>(boundedMaxTokens) > contextCapacity) {
      localError = LlamaError(14, @"The prompt and requested output exceed the GGUF context window.");
      return;
    }

    llama_memory_clear(llama_get_memory(context), true);
    const int32_t batchCapacity = static_cast<int32_t>(std::min<uint32_t>(llama_n_batch(context), 512));
    if (batchCapacity <= 0) {
      localError = LlamaError(15, @"The GGUF context reported an invalid batch size.");
      return;
    }
    BatchOwner batch(batchCapacity);
    if (batch.value.token == nullptr || batch.value.pos == nullptr || batch.value.n_seq_id == nullptr
        || batch.value.seq_id == nullptr || batch.value.logits == nullptr) {
      localError = LlamaError(26, @"llama.cpp could not allocate a generation batch.");
      return;
    }

    size_t offset = 0;
    while (offset < promptTokens.size()) {
      if (state->cancelRequested.load(std::memory_order_acquire)) {
        result = @{ @"text": @"", @"finishReason": @"cancelled", @"tokenCount": @0 };
        return;
      }
      const size_t remaining = promptTokens.size() - offset;
      const int32_t count = static_cast<int32_t>(std::min<size_t>(remaining, batchCapacity));
      const bool isFinalPromptBatch = offset + static_cast<size_t>(count) == promptTokens.size();
      FillBatch(batch.value, promptTokens.data() + offset, count, static_cast<int32_t>(offset),
                isFinalPromptBatch);
      const int32_t decodeStatus = llama_decode(context, batch.value);
      if (decodeStatus != 0) {
        localError = LlamaError(16, [NSString stringWithFormat:@"GGUF prompt decoding failed (%d).", decodeStatus]);
        return;
      }
      offset += static_cast<size_t>(count);
    }

    SamplerOwner sampler(llama_sampler_chain_init(llama_sampler_chain_default_params()));
    if (!sampler) {
      localError = LlamaError(17, @"llama.cpp could not create a sampler.");
      return;
    }
    if (temperature <= 0.0001) {
      if (!AddSampler(sampler.get(), llama_sampler_init_greedy(), &localError)) { return; }
    } else {
      if (!AddSampler(sampler.get(), llama_sampler_init_top_k(40), &localError)
          || !AddSampler(sampler.get(), llama_sampler_init_top_p(0.95f, 1), &localError)
          || !AddSampler(sampler.get(), llama_sampler_init_temp(static_cast<float>(temperature)),
                         &localError)
          || !AddSampler(sampler.get(), llama_sampler_init_dist(LLAMA_DEFAULT_SEED), &localError)) {
        return;
      }
    }

    std::vector<uint8_t> outputBytes;
    outputBytes.reserve(static_cast<size_t>(boundedMaxTokens) * 4);
    int32_t generated = 0;
    NSString *finishReason = @"length";
    int32_t position = static_cast<int32_t>(promptTokens.size());

    while (generated < boundedMaxTokens) {
      if (state->cancelRequested.load(std::memory_order_acquire)) {
        finishReason = @"cancelled";
        break;
      }

      const llama_token token = llama_sampler_sample(sampler.get(), context, -1);
      if (llama_vocab_is_eog(vocab, token)) {
        finishReason = @"stop";
        break;
      }
      llama_sampler_accept(sampler.get(), token);
      if (!AppendTokenPiece(vocab, token, outputBytes, &localError)) { return; }
      ++generated;

      if (generated == boundedMaxTokens) { break; }
      FillBatch(batch.value, &token, 1, position, true);
      ++position;
      const int32_t decodeStatus = llama_decode(context, batch.value);
      if (decodeStatus != 0) {
        localError = LlamaError(18, [NSString stringWithFormat:@"GGUF token decoding failed (%d).", decodeStatus]);
        return;
      }
    }

    NSData *data = [NSData dataWithBytes:outputBytes.data() length:outputBytes.size()];
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (text == nil) {
      localError = LlamaError(24, @"The GGUF model produced invalid UTF-8 output.");
      return;
    }
    result = @{ @"text": text, @"finishReason": finishReason, @"tokenCount": @(generated) };
  });

  if (result == nil) {
    if (error != nullptr) { *error = localError ?: LlamaError(19, @"Unknown GGUF inference error."); }
    return nil;
  }
  NSData *jsonData = [NSJSONSerialization dataWithJSONObject:result options:0 error:&localError];
  NSString *json = jsonData == nil ? nil : [[NSString alloc] initWithData:jsonData encoding:NSUTF8StringEncoding];
  if (json == nil && error != nullptr) {
    *error = localError ?: LlamaError(25, @"The GGUF result could not be serialized.");
  }
  return json;
}

- (void)cancel {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  if (state != nullptr) { state->cancelRequested.store(true, std::memory_order_release); }
}

- (void)resetCancellation {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  if (state != nullptr) { state->cancelRequested.store(false, std::memory_order_release); }
}

- (void)unload {
  LlamaState *state = static_cast<LlamaState *>(_statePointer);
  if (state == nullptr) { return; }
  state->cancelRequested.store(true, std::memory_order_release);
  dispatch_sync(_inferenceQueue, ^{
    state->unload();
    state->cancelRequested.store(false, std::memory_order_release);
  });
}

@end
