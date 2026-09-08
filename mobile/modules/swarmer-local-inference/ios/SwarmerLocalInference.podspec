Pod::Spec.new do |s|
  s.name           = 'SwarmerLocalInference'
  s.version        = '1.0.0'
  s.summary        = 'On-device Core ML, MLX, and llama.cpp inference for Swarmer.'
  s.description    = 'A private Expo module that owns bounded local language-model runtimes.'
  s.author         = '27PM'
  s.homepage       = 'https://github.com/ales27pm/swarmer'
  s.license        = { :type => 'MIT', :file => '../LICENSE' }
  s.platforms      = { :ios => '18.0' }
  s.source         = { :git => 'https://github.com/ales27pm/swarmer.git', :tag => s.version.to_s }
  s.static_framework = true

  s.dependency 'ExpoModulesCore'
  s.dependency 'SwarmerLlamaBinary', '10809.0.0'

  spm_dependency(
    s,
    url: 'https://github.com/huggingface/swift-transformers.git',
    requirement: { :kind => 'exactVersion', :version => '1.3.0' },
    products: ['Tokenizers']
  )
  spm_dependency(
    s,
    url: 'https://github.com/huggingface/swift-huggingface.git',
    requirement: { :kind => 'exactVersion', :version => '0.9.0' },
    products: ['HuggingFace']
  )
  spm_dependency(
    s,
    url: 'https://github.com/ml-explore/mlx-swift.git',
    requirement: { :kind => 'exactVersion', :version => '0.31.4' },
    products: ['MLX']
  )
  spm_dependency(
    s,
    url: 'https://github.com/ml-explore/mlx-swift-lm.git',
    requirement: { :kind => 'exactVersion', :version => '3.31.4' },
    products: ['MLXLLM', 'MLXLMCommon', 'MLXHuggingFace']
  )
  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    'SWIFT_VERSION' => '6.0',
    'SWIFT_STRICT_CONCURRENCY' => 'complete',
    'SWIFT_DEFAULT_ACTOR_ISOLATION' => 'nonisolated',
    'CLANG_CXX_LANGUAGE_STANDARD' => 'c++20'
  }

  s.frameworks = 'Accelerate', 'CoreML', 'Foundation', 'Metal'
  s.source_files = "*.{h,m,mm,swift,hpp,cpp}"
  s.public_header_files = 'SwarmerLlamaBridge.h'
end
