Pod::Spec.new do |s|
  s.name             = 'SwarmerLlamaBinary'
  s.version          = '10809.0.0'
  s.summary          = 'Pinned upstream llama.cpp XCFramework for Swarmer.'
  s.description      = 'The official llama.cpp b10809 XCFramework, fetched over HTTPS and verified by SHA-256.'
  s.author           = 'ggml-org and contributors'
  s.homepage         = 'https://github.com/ggml-org/llama.cpp'
  s.license          = {
    :type => 'MIT',
    :text => <<-LICENSE
MIT License

Copyright (c) 2023-2026 The ggml authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
    LICENSE
  }
  s.platform         = :ios, '16.4'
  s.source           = {
    :http => 'https://github.com/ggml-org/llama.cpp/releases/download/b10809/llama-b10809-xcframework.zip',
    :sha256 => 'd6813b3b6c73728a19f0bc0d1d7cea04ccdb07f9583c1d930c0b38af2377606d'
  }
  s.vendored_frameworks = 'build-apple/llama.xcframework'
end
