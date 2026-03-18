import { log } from './utils/logger.js';
import { webSocketConnect } from './xiaoZhiConnect.js';
import { checkOpusLoaded, initOpusEncoder } from './opus.js';
import { addMessage } from './document.js';
import BlockingQueue from './utils/BlockingQueue.js';
import { createStreamingContext } from './StreamingContext.js';

// 需要加载的脚本列表 - 移除Opus依赖
const scriptFiles = [];

// 脚本加载状态
const scriptStatus = {
  loading: 0,
  loaded: 0,
  failed: 0,
  total: scriptFiles.length
};

// 全局变量
let websocket = null;
let mediaRecorder = null;
let audioContext = null;
let analyser = null;
let audioChunks = [];
let isRecording = false;
let visualizerCanvas = document.getElementById('audioVisualizer');
let visualizerContext = visualizerCanvas.getContext('2d');
let audioQueue = [];
let isPlaying = false;
let opusDecoder = null; // Opus解码器
let visualizationRequest = null; // 动画帧请求ID

// 音频流缓冲相关
let audioBuffers = []; // 用于存储接收到的所有音频数据
let totalAudioSize = 0; // 跟踪累积的音频大小

let audioBufferQueue = []; // 存储接收到的音频包
let isAudioPlaying = false; // 是否正在播放音频
const BUFFER_THRESHOLD = 3; // 缓冲包数量阈值，至少累积3个包再开始播放
const MIN_AUDIO_DURATION = 0.1; // 最小音频长度(秒)，小于这个长度的音频会被合并
let streamingContext = null; // 音频流上下文
const SAMPLE_RATE = 16000; // 采样率
const CHANNELS = 1; // 声道数
const FRAME_SIZE = 960; // 帧大小

// DOM元素
const connectButton = document.getElementById('connectButton');
const serverUrlInput = document.getElementById('serverUrl');
const connectionStatus = document.getElementById('connectionStatus');
const messageInput = document.getElementById('messageInput');
const sendTextButton = document.getElementById('sendTextButton');
const recordButton = document.getElementById('recordButton');
const stopButton = document.getElementById('stopButton');
const conversationDiv = document.getElementById('conversation');
const logContainer = document.getElementById('logContainer');

function getAudioContextInstance() {
  if (!audioContext) {
    audioContext = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: SAMPLE_RATE,
      latencyHint: 'interactive'
    });
    log('创建音频上下文，采样率: ' + SAMPLE_RATE + 'Hz', 'debug');
  }
  return audioContext;
}

// 初始化可视化器
function initVisualizer() {
  visualizerCanvas.width = visualizerCanvas.clientWidth;
  visualizerCanvas.height = visualizerCanvas.clientHeight;
  visualizerContext.fillStyle = '#fafafa';
  visualizerContext.fillRect(0, 0, visualizerCanvas.width, visualizerCanvas.height);
}

// 绘制音频可视化效果
function drawVisualizer(dataArray) {
  visualizationRequest = requestAnimationFrame(() => drawVisualizer(dataArray));

  if (!isRecording) return;

  analyser.getByteFrequencyData(dataArray);

  visualizerContext.fillStyle = '#fafafa';
  visualizerContext.fillRect(0, 0, visualizerCanvas.width, visualizerCanvas.height);

  const barWidth = (visualizerCanvas.width / dataArray.length) * 2.5;
  let barHeight;
  let x = 0;

  for (let i = 0; i < dataArray.length; i++) {
    barHeight = dataArray[i] / 2;

    visualizerContext.fillStyle = `rgb(${barHeight + 100}, 50, 50)`;
    visualizerContext.fillRect(x, visualizerCanvas.height - barHeight, barWidth, barHeight);

    x += barWidth + 1;
  }
}

const queue = new BlockingQueue();

// 启动缓存进程
async function startAudioBuffering() {
  log('开始音频缓冲...', 'info');

  // 先尝试初始化解码器，以便在播放时已准备好
  initOpusDecoder().catch(error => {
    log(`预初始化Opus解码器失败: ${error.message}`, 'warning');
    // 继续缓冲，我们会在播放时再次尝试初始化
  });
  const timeout = 300;
  while (true) {
    // 每次数据空的时候等三条数据
    const packets = await queue.dequeue(
      3,
      timeout,
      (count) => {
        log(`缓冲超时，当前缓冲包数: ${count}，开始播放`, 'info');
      }
    );
    if (packets.length) {
      log(`已缓冲 ${packets.length} 个音频包，开始播放`, 'info');
      streamingContext.pushAudioBuffer(packets);
    }
    // 50毫秒里，有多少给多少
    while (true) {
      const data = await queue.dequeue(99, 50);
      if (data.length) {
        streamingContext.pushAudioBuffer(data);
      } else {
        break;
      }
    }
  }
}

// 播放已缓冲的音频
async function playBufferedAudio() {
  // 确保Opus解码器已初始化
  try {
    // 确保音频上下文存在
    audioContext = getAudioContextInstance();

    // 确保解码器已初始化
    if (!opusDecoder) {
      log('初始化Opus解码器...', 'info');
      try {
        opusDecoder = await initOpusDecoder();
        if (!opusDecoder) {
          throw new Error('解码器初始化失败');
        }
        log('Opus解码器初始化成功', 'success');
      } catch (error) {
        log('Opus解码器初始化失败: ' + error.message, 'error');
        isAudioPlaying = false;
        return;
      }
    }

    // 创建流式播放上下文
    if (!streamingContext) {
      streamingContext = createStreamingContext(opusDecoder, audioContext, SAMPLE_RATE, CHANNELS, MIN_AUDIO_DURATION);
    }

    streamingContext.decodeOpusFrames();
    streamingContext.startPlaying();
  } catch (error) {
    log(`播放已缓冲的音频出错: ${error.message}`, 'error');
    isAudioPlaying = false;
    streamingContext = null;
  }
}

// 初始化Opus解码器 - 确保完全初始化完成后才返回
async function initOpusDecoder() {
  if (opusDecoder) return opusDecoder; // 已经初始化

  try {
    // 检查ModuleInstance是否存在
    if (typeof window.ModuleInstance === 'undefined') {
      if (typeof Module !== 'undefined') {
        // 使用全局Module作为ModuleInstance
        window.ModuleInstance = Module;
        log('使用全局Module作为ModuleInstance', 'info');
      } else {
        throw new Error('Opus库未加载，ModuleInstance和Module对象都不存在');
      }
    }

    const mod = window.ModuleInstance;

    // 创建解码器对象
    opusDecoder = {
      channels: CHANNELS,
      rate: SAMPLE_RATE,
      frameSize: FRAME_SIZE,
      module: mod,
      decoderPtr: null,

      // 初始化解码器
      init: function () {
        if (this.decoderPtr) return true;

        // 获取解码器大小
        const decoderSize = mod._opus_decoder_get_size(this.channels);
        log(`Opus解码器大小: ${decoderSize}字节`, 'debug');

        // 分配内存
        this.decoderPtr = mod._malloc(decoderSize);
        if (!this.decoderPtr) {
          throw new Error('无法分配解码器内存');
        }

        // 初始化解码器
        const err = mod._opus_decoder_init(
          this.decoderPtr,
          this.rate,
          this.channels
        );

        if (err < 0) {
          this.destroy();
          throw new Error(`Opus解码器初始化失败: ${err}`);
        }

        log('Opus解码器初始化成功', 'success');
        return true;
      },

      // 解码方法
      decode: function (opusData) {
        if (!this.decoderPtr) {
          if (!this.init()) {
            throw new Error('解码器未初始化且无法初始化');
          }
        }

        try {
          const mod = this.module;

          // 为Opus数据分配内存
          const opusPtr = mod._malloc(opusData.length);
          mod.HEAPU8.set(opusData, opusPtr);

          // 为PCM输出分配内存
          const pcmPtr = mod._malloc(this.frameSize * 2);

          // 解码
          const decodedSamples = mod._opus_decode(
            this.decoderPtr,
            opusPtr,
            opusData.length,
            pcmPtr,
            this.frameSize,
            0
          );

          if (decodedSamples < 0) {
            mod._free(opusPtr);
            mod._free(pcmPtr);
            throw new Error(`Opus解码失败: ${decodedSamples}`);
          }

          // 复制解码后的数据
          const decodedData = new Int16Array(decodedSamples);
          for (let i = 0; i < decodedSamples; i++) {
            decodedData[i] = mod.HEAP16[(pcmPtr >> 1) + i];
          }

          // 释放内存
          mod._free(opusPtr);
          mod._free(pcmPtr);

          return decodedData;
        } catch (error) {
          log(`Opus解码错误: ${error.message}`, 'error');
          return new Int16Array(0);
        }
      },

      // 销毁方法
      destroy: function () {
        if (this.decoderPtr) {
          this.module._free(this.decoderPtr);
          this.decoderPtr = null;
        }
      }
    };

    if (!opusDecoder.init()) {
      throw new Error('Opus解码器初始化失败');
    }

    return opusDecoder;
  } catch (error) {
    log(`Opus解码器初始化失败: ${error.message}`, 'error');
    opusDecoder = null;
    throw error;
  }
}

// 初始化音频录制和处理
async function initAudio() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        sampleRate: 16000,
        channelCount: 1
      }
    });
    log('已获取麦克风访问权限', 'success');

    audioContext = getAudioContextInstance();
    const source = audioContext.createMediaStreamSource(stream);

    const audioTracks = stream.getAudioTracks();
    if (audioTracks.length > 0) {
      const track = audioTracks[0];
      const settings = track.getSettings();
      log(`实际麦克风设置 - 采样率: ${settings.sampleRate || '未知'}Hz, 声道数: ${settings.channelCount || '未知'}`, 'info');
    }

    analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    try {
      mediaRecorder = new MediaRecorder(stream, {
        mimeType: 'audio/webm;codecs=opus',
        audioBitsPerSecond: 16000
      });
      log('已初始化MediaRecorder (使用Opus编码)', 'success');
      log(`选择的编码格式: ${mediaRecorder.mimeType}`, 'info');
    } catch (e1) {
      try {
        mediaRecorder = new MediaRecorder(stream, {
          mimeType: 'audio/webm',
          audioBitsPerSecond: 16000
        });
        log('已初始化MediaRecorder (使用WebM标准编码，Opus不支持)', 'warning');
        log(`选择的编码格式: ${mediaRecorder.mimeType}`, 'info');
      } catch (e2) {
        try {
          mediaRecorder = new MediaRecorder(stream, {
            mimeType: 'audio/ogg;codecs=opus',
            audioBitsPerSecond: 16000
          });
          log('已初始化MediaRecorder (使用OGG+Opus编码)', 'warning');
          log(`选择的编码格式: ${mediaRecorder.mimeType}`, 'info');
        } catch (e3) {
          mediaRecorder = new MediaRecorder(stream);
          log(`已初始化MediaRecorder (使用默认编码: ${mediaRecorder.mimeType})`, 'warning');
        }
      }
    }

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        audioChunks.push(event.data);
      }
    };

    mediaRecorder.onstop = async () => {
      if (visualizationRequest) {
        cancelAnimationFrame(visualizationRequest);
        visualizationRequest = null;
      }

      log(`录音结束，已收集的音频块数量: ${audioChunks.length}`, 'info');
      if (audioChunks.length === 0) {
        log('警告：没有收集到任何音频数据，请检查麦克风是否工作正常', 'error');
        return;
      }

      const blob = new Blob(audioChunks, { type: audioChunks[0].type });
      log(`已创建音频Blob，MIME类型: ${audioChunks[0].type}，大小: ${(blob.size / 1024).toFixed(2)} KB`, 'info');

      const chunks = [...audioChunks];
      audioChunks = [];

      try {
        const arrayBuffer = await blob.arrayBuffer();
        const uint8Array = new Uint8Array(arrayBuffer);

        log(`已转换为Uint8Array，准备发送，大小: ${(arrayBuffer.byteLength / 1024).toFixed(2)} KB`, 'info');

        if (!websocket) {
          log('错误：WebSocket连接不存在', 'error');
          return;
        }

        if (websocket.readyState !== WebSocket.OPEN) {
          log(`错误：WebSocket连接未打开，当前状态: ${websocket.readyState}`, 'error');
          return;
        }

        try {
          await new Promise(resolve => setTimeout(resolve, 50));
          log('正在处理音频数据，提取纯Opus帧...', 'info');
          const opusData = extractOpusFrames(uint8Array);

          log(`已提取Opus数据，大小: ${(opusData.byteLength / 1024).toFixed(2)} KB`, 'info');
          websocket.send(opusData);
          log(`已发送Opus音频数据: ${(opusData.byteLength / 1024).toFixed(2)} KB`, 'success');
        } catch (error) {
          log(`音频数据发送失败: ${error.message}`, 'error');

          try {
            log('尝试使用base64编码方式发送...', 'info');
            const base64Data = arrayBufferToBase64(arrayBuffer);
            const audioDataMessage = {
              type: 'audio',
              action: 'data',
              format: 'opus',
              sample_rate: 16000,
              channels: 1,
              mime_type: chunks[0].type,
              encoding: 'base64',
              data: base64Data
            };
            websocket.send(JSON.stringify(audioDataMessage));
            log(`已使用base64编码发送音频数据: ${(arrayBuffer.byteLength / 1024).toFixed(2)} KB`, 'warning');
          } catch (base64Error) {
            log(`所有数据发送方式均失败: ${base64Error.message}`, 'error');
          }
        }
      } catch (error) {
        log(`处理录音数据错误: ${error.message}`, 'error');
      }
    };

    try {
      if (typeof window.ModuleInstance === 'undefined') {
        throw new Error('Opus库未加载，ModuleInstance对象不存在');
      }

      if (typeof window.ModuleInstance._opus_decoder_get_size === 'function') {
        const testSize = window.ModuleInstance._opus_decoder_get_size(1);
        log(`Opus解码器测试成功，解码器大小: ${testSize} 字节`, 'success');
      } else {
        throw new Error('Opus解码函数未找到');
      }
    } catch (err) {
      log(`Opus解码器初始化警告: ${err.message}，将在需要时重试`, 'warning');
    }

    log('音频系统初始化完成', 'success');
    return true;
  } catch (error) {
    log(`音频初始化错误: ${error.message}`, 'error');
    return false;
  }
}

// 开始录音
function startRecording() {
  if (isRecording) return;

  try {
    log('请至少录制1-2秒钟的音频，确保采集到足够数据', 'info');

    const serverUrl = serverUrlInput.value.trim();
    let isXiaozhiNative = false;

    if (serverUrl.includes('xiaozhi') || serverUrl.includes('localhost') || serverUrl.includes('127.0.0.1')) {
      isXiaozhiNative = true;
      log('检测到小智原生服务器，使用标准listen协议', 'info');
    }

    startDirectRecording();
  } catch (error) {
    log(`录音启动错误: ${error.message}`, 'error');
  }
}

// 停止录音
function stopRecording() {
  if (!isRecording) return;

  try {
    stopDirectRecording();
  } catch (error) {
    log(`停止录音错误: ${error.message}`, 'error');
  }
}

// 连接WebSocket服务器
async function connectToServer() {
  const url = serverUrlInput.value.trim();
  const config = getConfig();
  log('正在检查OTA状态...', 'info');
  const otaUrl = document.getElementById('otaUrl').value.trim();
  localStorage.setItem('otaUrl', otaUrl);
  localStorage.setItem('wsUrl', url);

  try {
    const ws = await webSocketConnect(otaUrl, url, config);
    if (ws === undefined) {
      return;
    }
    websocket = ws;

    websocket.binaryType = 'arraybuffer';

    websocket.onopen = async () => {
      log(`已连接到服务器: ${url}`, 'success');
      connectionStatus.textContent = 'ws已连接';
      connectionStatus.style.color = 'green';

      await sendHelloMessage();

      connectButton.textContent = '断开';
      connectButton.removeEventListener('click', connectToServer);
      connectButton.addEventListener('click', disconnectFromServer);
      messageInput.disabled = false;
      sendTextButton.disabled = false;

      const audioInitialized = await initAudio();
      if (audioInitialized) {
        recordButton.disabled = false;
      }
    };

    websocket.onclose = () => {
      log('已断开连接', 'info');
      connectionStatus.textContent = 'ws已断开';
      connectionStatus.style.color = 'red';

      connectButton.textContent = '连接';
      connectButton.removeEventListener('click', disconnectFromServer);
      connectButton.addEventListener('click', connectToServer);
      messageInput.disabled = true;
      sendTextButton.disabled = true;
      recordButton.disabled = true;
      stopButton.disabled = true;
    };

    websocket.onerror = (error) => {
      log(`WebSocket错误: ${error.message || '未知错误'}`, 'error');
      connectionStatus.textContent = 'ws未连接';
      connectionStatus.style.color = 'red';
    };

    websocket.onmessage = function (event) {
      try {
        if (typeof event.data === 'string') {
          const message = JSON.parse(event.data);

          if (message.type === 'hello') {
            log(`服务器回应：${JSON.stringify(message, null, 2)}`, 'success');
          } else if (message.type === 'tts') {
            if (message.state === 'start') {
              log('服务器开始发送语音', 'info');
            } else if (message.state === 'sentence_start') {
              log(`服务器发送语音段: ${message.text}`, 'info');
              if (message.text) {
                addMessage(message.text);
              }
            } else if (message.state === 'sentence_end') {
              log(`语音段结束: ${message.text}`, 'info');
            } else if (message.state === 'stop') {
              log('服务器语音传输结束', 'info');
              if (recordButton.disabled) {
                recordButton.disabled = false;
                recordButton.textContent = '开始录音';
                recordButton.classList.remove('recording');
              }
            }
          } else if (message.type === 'audio') {
            log(`收到音频控制消息: ${JSON.stringify(message)}`, 'info');
          } else if (message.type === 'stt') {
            log(`识别结果: ${message.text}`, 'info');
            addMessage(`[语音识别] ${message.text}`, true);
          } else if (message.type === 'llm') {
            log(`大模型回复: ${message.text}`, 'info');
            if (message.text && message.text !== '😊') {
              addMessage(message.text);
            }
          } else if (message.type === 'mcp') {
            const payload = message.payload || {};
            log(`服务器下发: ${JSON.stringify(message)}`, 'info');
            if (payload) {
              if (payload.method === 'tools/list') {
                const replay_message = JSON.stringify({
                  session_id: '',
                  type: 'mcp',
                  payload: {
                    jsonrpc: '2.0',
                    id: 2,
                    result: {
                      tools: [
                        {
                          name: 'self.get_device_status',
                          description: 'Provides the real-time information of the device, including the current status of the audio speaker, screen, battery, network, etc.\nUse this tool for: \n1. Answering questions about current condition (e.g. what is the current volume of the audio speaker?)\n2. As the first step to control the device (e.g. turn up / down the volume of the audio speaker, etc.)',
                          inputSchema: { type: 'object', properties: {} }
                        },
                        {
                          name: 'self.audio_speaker.set_volume',
                          description: 'Set the volume of the audio speaker. If the current volume is unknown, you must call `self.get_device_status` tool first and then call this tool.',
                          inputSchema: {
                            type: 'object',
                            properties: {
                              volume: {
                                type: 'integer',
                                minimum: 0,
                                maximum: 100
                              }
                            },
                            required: ['volume']
                          }
                        },
                        {
                          name: 'self.screen.set_brightness',
                          description: 'Set the brightness of the screen.',
                          inputSchema: {
                            type: 'object',
                            properties: {
                              brightness: {
                                type: 'integer',
                                minimum: 0,
                                maximum: 100
                              }
                            },
                            required: ['brightness']
                          }
                        },
                        {
                          name: 'self.screen.set_theme',
                          description: 'Set the theme of the screen. The theme can be \"light\" or \"dark\".',
                          inputSchema: {
                            type: 'object',
                            properties: { theme: { type: 'string' } },
                            required: ['theme']
                          }
                        }
                      ]
                    }
                  }
                });
                websocket.send(replay_message);
                log(`回复MCP消息: ${replay_message}`, 'info');
              } else if (payload.method === 'tools/call') {
                const replay_message = JSON.stringify({
                  session_id: '9f261599',
                  type: 'mcp',
                  payload: {
                    jsonrpc: '2.0',
                    id: payload.id,
                    result: { content: [{ type: 'text', text: 'true' }], isError: false }
                  }
                });
                websocket.send(replay_message);
                log(`回复MCP消息: ${replay_message}`, 'info');
              }
            }
          } else {
            log(`未知消息类型: ${message.type}`, 'info');
            addMessage(JSON.stringify(message, null, 2));
          }
        } else {
          handleBinaryMessage(event.data);
        }
      } catch (error) {
        log(`WebSocket消息处理错误: ${error.message}`, 'error');
        if (typeof event.data === 'string') {
          addMessage(event.data);
        }
      }
    };

    connectionStatus.textContent = 'ws未连接';
    connectionStatus.style.color = 'orange';
  } catch (error) {
    log(`连接错误: ${error.message}`, 'error');
    connectionStatus.textContent = 'ws未连接';
  }
}

// 发送hello握手消息
async function sendHelloMessage() {
  if (!websocket || websocket.readyState !== WebSocket.OPEN) return;

  try {
    const config = getConfig();

    const helloMessage = {
      type: 'hello',
      device_id: config.deviceId,
      device_name: config.deviceName,
      device_mac: config.deviceMac,
      token: config.token,
      features: {
        mcp: true
      }
    };

    log('发送hello握手消息', 'info');
    websocket.send(JSON.stringify(helloMessage));

    return new Promise(resolve => {
      const timeout = setTimeout(() => {
        log('等待hello响应超时', 'error');
        log('提示: 请尝试点击"测试认证"按钮进行连接排查', 'info');
        resolve(false);
      }, 5000);

      const onMessageHandler = (event) => {
        try {
          const response = JSON.parse(event.data);
          if (response.type === 'hello' && response.session_id) {
            log(`服务器握手成功，会话ID: ${response.session_id}`, 'success');
            clearTimeout(timeout);
            websocket.removeEventListener('message', onMessageHandler);
            resolve(true);
          }
        } catch (e) {
          // ignore non-JSON
        }
      };

      websocket.addEventListener('message', onMessageHandler);
    });
  } catch (error) {
    log(`发送hello消息错误: ${error.message}`, 'error');
    return false;
  }
}

// 断开WebSocket连接
function disconnectFromServer() {
  if (!websocket) return;

  websocket.close();
  stopRecording();
}

// 发送文本消息
function sendTextMessage() {
  const message = messageInput.value.trim();
  if (message === '' || !websocket || websocket.readyState !== WebSocket.OPEN) return;

  try {
    const listenMessage = {
      type: 'listen',
      mode: 'manual',
      state: 'detect',
      text: message
    };

    websocket.send(JSON.stringify(listenMessage));
    addMessage(message, true);
    log(`发送文本消息: ${message}`, 'info');

    messageInput.value = '';
  } catch (error) {
    log(`发送消息错误: ${error.message}`, 'error');
  }
}

// 生成随机MAC地址
function generateRandomMac() {
  const hexDigits = '0123456789ABCDEF';
  let mac = '';
  for (let i = 0; i < 6; i++) {
    if (i > 0) mac += ':';
    for (let j = 0; j < 2; j++) {
      mac += hexDigits.charAt(Math.floor(Math.random() * 16));
    }
  }
  return mac;
}

// 初始化事件监听器
function initEventListeners() {
  connectButton.addEventListener('click', connectToServer);
  document.getElementById('authTestButton').addEventListener('click', testAuthentication);

  const toggleButton = document.getElementById('toggleConfig');
  const configPanel = document.getElementById('configPanel');
  const deviceMacInput = document.getElementById('deviceMac');
  const clientIdInput = document.getElementById('clientId');
  const displayMac = document.getElementById('displayMac');
  const displayClient = document.getElementById('displayClient');

  let savedMac = localStorage.getItem('deviceMac');
  if (!savedMac) {
    savedMac = generateRandomMac();
    localStorage.setItem('deviceMac', savedMac);
  }
  deviceMacInput.value = savedMac;
  displayMac.textContent = savedMac;

  function updateDisplayValues() {
    const newMac = deviceMacInput.value;
    displayMac.textContent = newMac;
    displayClient.textContent = clientIdInput.value;
    localStorage.setItem('deviceMac', newMac);
  }

  deviceMacInput.addEventListener('input', updateDisplayValues);
  clientIdInput.addEventListener('input', updateDisplayValues);
  updateDisplayValues();

  const savedOtaUrl = localStorage.getItem('otaUrl');
  if (savedOtaUrl) {
    document.getElementById('otaUrl').value = savedOtaUrl;
  }

  const savedWsUrl = localStorage.getItem('wsUrl');
  if (savedWsUrl) {
    document.getElementById('serverUrl').value = savedWsUrl;
  }

  toggleButton.addEventListener('click', () => {
    const isExpanded = configPanel.classList.contains('expanded');
    configPanel.classList.toggle('expanded');
    toggleButton.textContent = isExpanded ? '编辑' : '收起';
  });

  const tabs = document.querySelectorAll('.tab');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(`${tab.dataset.tab}Tab`).classList.add('active');
    });
  });

  sendTextButton.addEventListener('click', sendTextMessage);
  messageInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendTextMessage();
  });

  recordButton.addEventListener('click', () => {
    if (isRecording) {
      stopRecording();
    } else {
      startRecording();
    }
  });

  window.addEventListener('resize', initVisualizer);
}

// 测试认证
async function testAuthentication() {
  log('开始测试认证...', 'info');

  const config = getConfig();

  log('-------- 服务器认证配置检查 --------', 'info');
  log('请确认config.yaml中的auth配置：', 'info');
  log('1. server.auth.enabled 为 false 或服务器已正确配置认证', 'info');
  log('2. 如果启用了认证，请确认使用了正确的token', 'info');
  log(`3. 或者在allowed_devices中添加了测试设备MAC：${config.deviceMac}`, 'info');

  const serverUrl = serverUrlInput.value.trim();
  if (!serverUrl) {
    log('请输入服务器地址', 'error');
    return;
  }

  log('尝试不同认证参数的连接：', 'info');

  try {
    log('测试1: 尝试无参数连接...', 'info');
    const ws1 = new WebSocket(serverUrl);

    ws1.onopen = () => {
      log('测试1成功: 无参数可连接，服务器可能没有启用认证', 'success');
      ws1.close();
    };

    ws1.onerror = () => {
      log('测试1失败: 无参数连接被拒绝，服务器可能启用了认证', 'error');
    };

    setTimeout(() => {
      if (ws1.readyState === WebSocket.CONNECTING || ws1.readyState === WebSocket.OPEN) {
        ws1.close();
      }
    }, 5000);
  } catch (error) {
    log(`测试1出错: ${error.message}`, 'error');
  }

  setTimeout(async () => {
    try {
      log('测试2: 尝试带token参数连接...', 'info');

      let url = new URL(serverUrl);
      url.searchParams.append('token', config.token);
      url.searchParams.append('device_id', config.deviceId);
      url.searchParams.append('device_mac', config.deviceMac);

      const ws2 = new WebSocket(url.toString());

      ws2.onopen = () => {
        log('测试2成功: 带token参数可连接', 'success');

        const helloMsg = {
          type: 'hello',
          device_id: config.deviceId,
          device_mac: config.deviceMac,
          token: config.token
        };

        ws2.send(JSON.stringify(helloMsg));
        log('已发送hello测试消息', 'info');

        ws2.onmessage = (event) => {
          try {
            const response = JSON.parse(event.data);
            if (response.type === 'hello' && response.session_id) {
              log(`测试完全成功! 收到hello响应，会话ID: ${response.session_id}`, 'success');
              ws2.close();
            }
          } catch (e) {
            log(`收到非JSON响应: ${event.data}`, 'info');
          }
        };

        setTimeout(() => ws2.close(), 5000);
      };

      ws2.onerror = () => {
        log('测试2失败: 带token参数连接被拒绝', 'error');
        log('请检查token是否正确，或服务器是否接受URL参数认证', 'error');
      };
    } catch (error) {
      log(`测试2出错: ${error.message}`, 'error');
    }
  }, 6000);

  log('认证测试已启动，请查看测试结果...', 'info');
}

// 从Uint8Array中提取（或直接返回）Opus帧。
// 目前直接透传数据，因为MediaRecorder通常已输出Opus帧；如果后续需要精确解析WebM容器，可在此扩展。
function extractOpusFrames(uint8Array) {
  return uint8Array;
}

// 帮助函数：ArrayBuffer转Base64
function arrayBufferToBase64(buffer) {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  const len = bytes.byteLength;
  for (let i = 0; i < len; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return window.btoa(binary);
}

let opusEncoder;

// 初始化应用
function initApp() {
  initVisualizer();
  initEventListeners();

  checkOpusLoaded();

  opusEncoder = initOpusEncoder();

  log('预加载Opus解码器...', 'info');
  initOpusDecoder().then(() => {
    log('Opus解码器预加载成功', 'success');
  }).catch(error => {
    log(`Opus解码器预加载失败: ${error.message}，将在需要时重试`, 'warning');
  });
  playBufferedAudio();
  startAudioBuffering();
}

const audioProcessorCode = `
            class AudioRecorderProcessor extends AudioWorkletProcessor {
                constructor() {
                    super();
                    this.buffers = [];
                    this.frameSize = 960; // 60ms @ 16kHz = 960 samples
                    this.buffer = new Int16Array(this.frameSize);
                    this.bufferIndex = 0;
                    this.isRecording = false;

                    // 监听来自主线程的消息
                    this.port.onmessage = (event) => {
                        if (event.data.command === 'start') {
                            this.isRecording = true;
                            this.port.postMessage({ type: 'status', status: 'started' });
                        } else if (event.data.command === 'stop') {
                            this.isRecording = false;

                            // 发送剩余的缓冲区
                            if (this.bufferIndex > 0) {
                                const finalBuffer = this.buffer.slice(0, this.bufferIndex);
                                this.port.postMessage({
                                    type: 'buffer',
                                    buffer: finalBuffer
                                });
                                this.bufferIndex = 0;
                            }

                            this.port.postMessage({ type: 'status', status: 'stopped' });
                        }
                    };
                }

                process(inputs, outputs, parameters) {
                    if (!this.isRecording) return true;

                    const input = inputs[0][0]; // 获取第一个输入通道
                    if (!input) return true;

                    // 将浮点采样转换为16位整数并存储
                    for (let i = 0; i < input.length; i++) {
                        if (this.bufferIndex >= this.frameSize) {
                            // 缓冲区已满，发送给主线程并重置
                            this.port.postMessage({
                                type: 'buffer',
                                buffer: this.buffer.slice(0)
                            });
                            this.bufferIndex = 0;
                        }

                        // 转换为16位整数 (-32768到32767)
                        this.buffer[this.bufferIndex++] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
                    }

                    return true;
                }
            }

            registerProcessor('audio-recorder-processor', AudioRecorderProcessor);
        `;

// 创建音频处理器
async function createAudioProcessor() {
  audioContext = getAudioContextInstance();

  try {
    if (audioContext.audioWorklet) {
      const blob = new Blob([audioProcessorCode], { type: 'application/javascript' });
      const url = URL.createObjectURL(blob);
      await audioContext.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);

      const audioProcessor = new AudioWorkletNode(audioContext, 'audio-recorder-processor');

      audioProcessor.port.onmessage = (event) => {
        if (event.data.type === 'buffer') {
          processPCMBuffer(event.data.buffer);
        }
      };

      log('使用AudioWorklet处理音频', 'success');
      return { node: audioProcessor, type: 'worklet' };
    } else {
      log('AudioWorklet不可用，使用ScriptProcessorNode作为回退方案', 'warning');

      const frameSize = 4096;
      const scriptProcessor = audioContext.createScriptProcessor(frameSize, 1, 1);

      scriptProcessor.onaudioprocess = (event) => {
        if (!isRecording) return;

        const input = event.inputBuffer.getChannelData(0);
        const buffer = new Int16Array(input.length);

        for (let i = 0; i < input.length; i++) {
          buffer[i] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
        }

        processPCMBuffer(buffer);
      };

      const silent = audioContext.createGain();
      silent.gain.value = 0;
      scriptProcessor.connect(silent);
      silent.connect(audioContext.destination);

      return { node: scriptProcessor, type: 'processor' };
    }
  } catch (error) {
    log(`创建音频处理器失败: ${error.message}，尝试回退方案`, 'error');

    try {
      const frameSize = 4096;
      const scriptProcessor = audioContext.createScriptProcessor(frameSize, 1, 1);

      scriptProcessor.onaudioprocess = (event) => {
        if (!isRecording) return;

        const input = event.inputBuffer.getChannelData(0);
        const buffer = new Int16Array(input.length);

        for (let i = 0; i < input.length; i++) {
          buffer[i] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
        }

        processPCMBuffer(buffer);
      };

      const silent = audioContext.createGain();
      silent.gain.value = 0;
      scriptProcessor.connect(silent);
      silent.connect(audioContext.destination);

      log('使用ScriptProcessorNode作为回退方案成功', 'warning');
      return { node: scriptProcessor, type: 'processor' };
    } catch (fallbackError) {
      log(`回退方案也失败: ${fallbackError.message}`, 'error');
      return null;
    }
  }
}

let audioProcessor = null;
let audioProcessorType = null;
let audioSource = null;

let pcmDataBuffer = new Int16Array();

function processPCMBuffer(buffer) {
  if (!isRecording) return;

  const newBuffer = new Int16Array(pcmDataBuffer.length + buffer.length);
  newBuffer.set(pcmDataBuffer);
  newBuffer.set(buffer, pcmDataBuffer.length);
  pcmDataBuffer = newBuffer;

  const samplesPerFrame = 960;

  while (pcmDataBuffer.length >= samplesPerFrame) {
    const frameData = pcmDataBuffer.slice(0, samplesPerFrame);
    pcmDataBuffer = pcmDataBuffer.slice(samplesPerFrame);

    encodeAndSendOpus(frameData);
  }
}

// 编码并发送Opus数据
function encodeAndSendOpus(pcmData = null) {
  if (!opusEncoder) {
    log('Opus编码器未初始化', 'error');
    return;
  }

  try {
    if (pcmData) {
      const opusData = opusEncoder.encode(pcmData);

      if (opusData && opusData.length > 0) {
        audioBuffers.push(opusData.buffer);
        totalAudioSize += opusData.length;

        if (websocket && websocket.readyState === WebSocket.OPEN) {
          try {
            websocket.send(opusData.buffer);
            log(`发送Opus帧，大小：${opusData.length}字节`, 'debug');
          } catch (error) {
            log(`WebSocket发送错误: ${error.message}`, 'error');
          }
        }
      } else {
        log('Opus编码失败，无有效数据返回', 'error');
      }
    } else {
      if (pcmDataBuffer.length > 0) {
        const samplesPerFrame = 960;
        if (pcmDataBuffer.length < samplesPerFrame) {
          const paddedBuffer = new Int16Array(samplesPerFrame);
          paddedBuffer.set(pcmDataBuffer);
          encodeAndSendOpus(paddedBuffer);
        } else {
          encodeAndSendOpus(pcmDataBuffer.slice(0, samplesPerFrame));
        }
        pcmDataBuffer = new Int16Array(0);
      }
    }
  } catch (error) {
    log(`Opus编码错误: ${error.message}`, 'error');
  }
}

// 开始直接从PCM数据录音
async function startDirectRecording() {
  if (isRecording) return;

  try {
    if (!initOpusEncoder()) {
      log('无法启动录音: Opus编码器初始化失败', 'error');
      return;
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        sampleRate: 16000,
        channelCount: 1
      }
    });

    audioContext = getAudioContextInstance();

    const processorResult = await createAudioProcessor();
    if (!processorResult) {
      log('无法创建音频处理器', 'error');
      return;
    }

    audioProcessor = processorResult.node;
    audioProcessorType = processorResult.type;

    audioSource = audioContext.createMediaStreamSource(stream);
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;

    audioSource.connect(analyser);
    audioSource.connect(audioProcessor);

    pcmDataBuffer = new Int16Array();
    audioBuffers = [];
    totalAudioSize = 0;
    isRecording = true;

    if (audioProcessorType === 'worklet' && audioProcessor.port) {
      audioProcessor.port.postMessage({ command: 'start' });
    }

    if (websocket && websocket.readyState === WebSocket.OPEN) {
      const listenMessage = {
        type: 'listen',
        mode: 'manual',
        state: 'start'
      };

      log(`发送录音开始消息: ${JSON.stringify(listenMessage)}`, 'info');
      websocket.send(JSON.stringify(listenMessage));
    } else {
      log('WebSocket未连接，无法发送开始消息', 'error');
      return false;
    }

    const dataArray = new Uint8Array(analyser.frequencyBinCount);
    drawVisualizer(dataArray);

    let recordingSeconds = 0;
    const recordingTimer = setInterval(() => {
      recordingSeconds += 0.1;
      recordButton.textContent = `停止录音 ${recordingSeconds.toFixed(1)}秒`;
    }, 100);

    window.recordingTimer = recordingTimer;

    recordButton.classList.add('recording');
    recordButton.disabled = false;

    log('开始PCM直接录音', 'success');
    return true;
  } catch (error) {
    log(`直接录音启动错误: ${error.message}`, 'error');
    isRecording = false;
    return false;
  }
}

// 停止直接从PCM数据录音
function stopDirectRecording() {
  if (!isRecording) return;

  try {
    isRecording = false;

    if (audioProcessor) {
      if (audioProcessorType === 'worklet' && audioProcessor.port) {
        audioProcessor.port.postMessage({ command: 'stop' });
      }

      audioProcessor.disconnect();
      audioProcessor = null;
    }

    if (audioSource) {
      audioSource.disconnect();
      audioSource = null;
    }

    if (visualizationRequest) {
      cancelAnimationFrame(visualizationRequest);
      visualizationRequest = null;
    }

    if (window.recordingTimer) {
      clearInterval(window.recordingTimer);
      window.recordingTimer = null;
    }

    encodeAndSendOpus();

    if (websocket && websocket.readyState === WebSocket.OPEN) {
      const emptyOpusFrame = new Uint8Array(0);
      websocket.send(emptyOpusFrame);

      const stopMessage = {
        type: 'listen',
        mode: 'manual',
        state: 'stop'
      };

      websocket.send(JSON.stringify(stopMessage));
      log('已发送录音停止信号', 'info');
    }

    recordButton.textContent = '开始录音';
    recordButton.classList.remove('recording');
    recordButton.disabled = false;

    log('停止PCM直接录音', 'success');
    return true;
  } catch (error) {
    log(`直接录音停止错误: ${error.message}`, 'error');
    return false;
  }
}

async function handleBinaryMessage(data) {
  try {
    let arrayBuffer;
    if (data instanceof ArrayBuffer) {
      arrayBuffer = data;
      log(`收到ArrayBuffer音频数据，大小: ${data.byteLength}字节`, 'debug');
    } else if (data instanceof Blob) {
      arrayBuffer = await data.arrayBuffer();
      log(`收到Blob音频数据，大小: ${arrayBuffer.byteLength}字节`, 'debug');
    } else {
      log(`收到未知类型的二进制数据: ${typeof data}`, 'warning');
      return;
    }
    const opusData = new Uint8Array(arrayBuffer);
    if (opusData.length > 0) {
      queue.enqueue(opusData);
    } else {
      log('收到空音频数据帧，可能是结束标志', 'warning');
      if (isAudioPlaying && streamingContext) {
        streamingContext.endOfStream = true;
      }
    }
  } catch (error) {
    log(`处理二进制消息出错: ${error.message}`, 'error');
  }
}

function getConfig() {
  const deviceMac = document.getElementById('deviceMac').value.trim();
  return {
    deviceId: deviceMac,
    deviceName: document.getElementById('deviceName').value.trim(),
    deviceMac: deviceMac,
    clientId: document.getElementById('clientId').value.trim(),
    token: document.getElementById('token').value.trim()
  };
}

initApp();
