import sys
import wave
import argparse
import os

# 检查依赖库
try:
    import opuslib
except ImportError:
    print("错误: 未找到 opuslib 库。")
    print("请运行 install: pip install opuslib")
    print("Windows 用户请确保 libopus-0.dll 在当前目录或系统目录中。")
    sys.exit(1)

# ================= 核心配置 (针对 QCC5181) =================
# 如果解出来的声音不对（如变调），请尝试修改 FRAME_DURATION_MS 为 10
SAMPLE_RATE = 16000        # 16kHz
CHANNELS = 1               # 单声道
BITRATE = 32000            # 32kbps
FRAME_DURATION_MS = 20     # 默认帧长 20ms
# =========================================================

def decode_opus_file(input_file, output_file):
    # 1. 计算每帧大小
    # 帧采样数 = 16000 * 0.02 = 320
    frame_size_samples = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0))
    
    # 每帧字节数 (CBR模式) = 32000 * 0.02 / 8 = 80 bytes
    packet_size_bytes = int(BITRATE * (FRAME_DURATION_MS / 1000.0) / 8)

    print(f"[-] 配置: {SAMPLE_RATE}Hz, {BITRATE}bps, {FRAME_DURATION_MS}ms 帧长")
    print(f"[-] 计算包大小: {packet_size_bytes} 字节/帧")
    
    if not os.path.exists(input_file):
        print(f"[!] 错误: 输入文件 '{input_file}' 不存在")
        return

    try:
        decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
    except Exception as e:
        print(f"[!] 解码器初始化失败: {e}")
        print("提示: Windows下通常是因为找不到 libopus-0.dll")
        return

    pcm_data = bytearray()
    packet_count = 0

    try:
        with open(input_file, 'rb') as f_in:
            while True:
                # 读取一帧裸数据
                encoded_packet = f_in.read(packet_size_bytes)
                
                if len(encoded_packet) < packet_size_bytes:
                    break # 数据读完
                
                try:
                    # 解码
                    decoded_frame = decoder.decode(encoded_packet, frame_size_samples)
                    pcm_data.extend(decoded_frame)
                    packet_count += 1
                except opuslib.OpusError as e:
                    print(f"[!] 第 {packet_count} 帧解码错误: {e}")
                    # 出错时可以选择插入静音，这里简单跳过
                    continue
                    
        # 保存 WAV
        with wave.open(output_file, 'wb') as f_out:
            f_out.setnchannels(CHANNELS)
            f_out.setsampwidth(2) # 16-bit
            f_out.setframerate(SAMPLE_RATE)
            f_out.writeframes(pcm_data)
            
        duration = len(pcm_data) / (SAMPLE_RATE * 2)
        print(f"[+] 解码成功!")
        print(f"    - 输入: {input_file}")
        print(f"    - 输出: {output_file}")
        print(f"    - 总帧数: {packet_count}")
        print(f"    - 音频时长: {duration:.2f} 秒")

    except Exception as e:
        print(f"[!] 处理过程中发生未知错误: {e}")

if __name__ == "__main__":
    # 设置命令行参数解析
    parser = argparse.ArgumentParser(description="解码 QCC5181 Raw Opus (CELT 32kbps) 音频")
    parser.add_argument("input_file", help="输入的 Raw Opus 文件路径 (Flash Dump)")
    parser.add_argument("output_file", help="输出的 WAV 音频文件路径")
    
    args = parser.parse_args()
    
    decode_opus_file(args.input_file, args.output_file)