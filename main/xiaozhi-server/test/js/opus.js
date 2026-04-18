import { log } from './utils/logger.js';
import { updateScriptStatus } from './document.js'


// 检查Opus库是否已加载
export function checkOpusLoaded() {
    try {
        // 检查Module是否存在（本地库导出的全局变量）
        if (typeof Module === 'undefined') {
            throw new Error('Opus library is not loaded. Module is missing.');
        }

        // 尝试先使用Module.instance（libopus.js最后一行导出方式）
        if (typeof Module.instance !== 'undefined' && typeof Module.instance._opus_decoder_get_size === 'function') {
            // 使用Module.instance对象替换全局Module对象
            window.ModuleInstance = Module.instance;
            log('Opus library loaded successfully via Module.instance', 'success');
            updateScriptStatus('Opus library loaded successfully', 'success');

            // 3秒后隐藏状态
            const statusElement = document.getElementById('scriptStatus');
            if (statusElement) statusElement.style.display = 'none';
            return;
        }

        // 如果没有Module.instance，检查全局Module函数
        if (typeof Module._opus_decoder_get_size === 'function') {
            window.ModuleInstance = Module;
            log('Opus library loaded successfully via global Module', 'success');
            updateScriptStatus('Opus library loaded successfully', 'success');

            // 3秒后隐藏状态
            const statusElement = document.getElementById('scriptStatus');
            if (statusElement) statusElement.style.display = 'none';
            return;
        }

        throw new Error('Opus decoder function was not found. Module may be malformed.');
    } catch (err) {
        log(`Failed to load Opus library. Check whether libopus.js exists and is valid: ${err.message}`, 'error');
        updateScriptStatus('Failed to load Opus library', 'error');
    }
}


// 创建一个Opus编码器
let opusEncoder = null;
export function initOpusEncoder() {
    try {
        if (opusEncoder) {
            return opusEncoder; // 已经初始化过
        }

        if (!window.ModuleInstance) {
            log('Unable to create the Opus encoder: ModuleInstance is unavailable', 'error');
            return;
        }

        // 初始化一个Opus编码器
        const mod = window.ModuleInstance;
        const sampleRate = 16000; // 16kHz采样率
        const channels = 1;       // 单声道
        const application = 2048; // OPUS_APPLICATION_VOIP = 2048

        // 创建编码器
        opusEncoder = {
            channels: channels,
            sampleRate: sampleRate,
            frameSize: 960, // 60ms @ 16kHz = 60 * 16 = 960 samples
            maxPacketSize: 4000, // 最大包大小
            module: mod,

            // 初始化编码器
            init: function () {
                try {
                    // 获取编码器大小
                    const encoderSize = mod._opus_encoder_get_size(this.channels);
                    log(`Opus encoder size: ${encoderSize} bytes`, 'info');

                    // 分配内存
                    this.encoderPtr = mod._malloc(encoderSize);
                    if (!this.encoderPtr) {
                        throw new Error("Failed to allocate encoder memory");
                    }

                    // 初始化编码器
                    const err = mod._opus_encoder_init(
                        this.encoderPtr,
                        this.sampleRate,
                        this.channels,
                        application
                    );

                    if (err < 0) {
                        throw new Error(`Opus encoder initialization failed: ${err}`);
                    }

                    // 设置位率 (16kbps)
                    mod._opus_encoder_ctl(this.encoderPtr, 4002, 16000); // OPUS_SET_BITRATE

                    // 设置复杂度 (0-10, 越高质量越好但CPU使用越多)
                    mod._opus_encoder_ctl(this.encoderPtr, 4010, 5);     // OPUS_SET_COMPLEXITY

                    // 设置使用DTX (不传输静音帧)
                    mod._opus_encoder_ctl(this.encoderPtr, 4016, 1);     // OPUS_SET_DTX

                    log("Opus encoder initialized successfully", 'success');
                    return true;
                } catch (error) {
                    if (this.encoderPtr) {
                        mod._free(this.encoderPtr);
                        this.encoderPtr = null;
                    }
                    log(`Opus encoder initialization failed: ${error.message}`, 'error');
                    return false;
                }
            },

            // 编码PCM数据为Opus
            encode: function (pcmData) {
                if (!this.encoderPtr) {
                    if (!this.init()) {
                        return null;
                    }
                }

                try {
                    const mod = this.module;

                    // 为PCM数据分配内存
                    const pcmPtr = mod._malloc(pcmData.length * 2); // 2字节/int16

                    // 将PCM数据复制到HEAP
                    for (let i = 0; i < pcmData.length; i++) {
                        mod.HEAP16[(pcmPtr >> 1) + i] = pcmData[i];
                    }

                    // 为输出分配内存
                    const outPtr = mod._malloc(this.maxPacketSize);

                    // 进行编码
                    const encodedLen = mod._opus_encode(
                        this.encoderPtr,
                        pcmPtr,
                        this.frameSize,
                        outPtr,
                        this.maxPacketSize
                    );

                    if (encodedLen < 0) {
                        throw new Error(`Opus encoding failed: ${encodedLen}`);
                    }

                    // 复制编码后的数据
                    const opusData = new Uint8Array(encodedLen);
                    for (let i = 0; i < encodedLen; i++) {
                        opusData[i] = mod.HEAPU8[outPtr + i];
                    }

                    // 释放内存
                    mod._free(pcmPtr);
                    mod._free(outPtr);

                    return opusData;
                } catch (error) {
                    log(`Opus encoding error: ${error.message}`, 'error');
                    return null;
                }
            },

            // 销毁编码器
            destroy: function () {
                if (this.encoderPtr) {
                    this.module._free(this.encoderPtr);
                    this.encoderPtr = null;
                }
            }
        };

        opusEncoder.init();
        return opusEncoder;
    } catch (error) {
        log(`Failed to create the Opus encoder: ${error.message}`, 'error');
        return false;
    }
}
