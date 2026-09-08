#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Owns llama.cpp pointers on a private serial queue. Cancellation only flips an atomic flag,
/// so it remains safe to call while Metal or CPU decoding is in progress.
@interface SwarmerLlamaBridge : NSObject

@property(nonatomic, readonly, getter=isLoaded) BOOL loaded;
@property(nonatomic, readonly, getter=isBusy) BOOL busy;

- (BOOL)loadModelAtPath:(NSString *)path
            contextSize:(NSInteger)contextSize
                  error:(NSError * _Nullable * _Nullable)error;

/// Returns a UTF-8 JSON envelope containing text, finishReason, and tokenCount. A String return
/// keeps the Swift concurrency boundary value-semantic and Sendable.
- (nullable NSString *)generatePrompt:(NSString *)prompt
                             maxTokens:(NSInteger)maxTokens
                           temperature:(double)temperature
                                 error:(NSError * _Nullable * _Nullable)error;

/// Clear a previous operation's cancellation before its worker is enqueued. A later cancellation
/// is never reset by the worker itself.
- (void)resetCancellation;
- (void)cancel;
- (void)unload;

@end

NS_ASSUME_NONNULL_END
