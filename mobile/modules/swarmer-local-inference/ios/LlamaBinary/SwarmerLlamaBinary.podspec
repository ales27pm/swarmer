Pod::Spec.new do |s|
  s.name             = 'SwarmerLlamaBinary'
  s.version          = '10809.0.0'
  s.summary          = 'Pinned upstream llama.cpp XCFramework for Swarmer.'
  s.description      = 'The official llama.cpp b10809 XCFramework, fetched over HTTPS and verified by SHA-256.'
  s.author           = 'ggml-org and contributors'
  s.homepage         = 'https://github.com/ggml-org/llama.cpp'
  s.license          = { :type => 'MIT' }
  s.platform         = :ios, '16.4'
  s.source           = {
    :http => 'https://github.com/ggml-org/llama.cpp/releases/download/b10809/llama-b10809-xcframework.zip',
    :sha256 => 'd6813b3b6c73728a19f0bc0d1d7cea04ccdb07f9583c1d930c0b38af2377606d'
  }
  s.vendored_frameworks = 'build-apple/llama.xcframework'
end
