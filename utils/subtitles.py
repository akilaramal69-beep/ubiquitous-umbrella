import os
import asyncio
import time
from faster_whisper import WhisperModel
from plugins.config import Config
from groq import AsyncGroq
import openai
import json
import re

# Cache for local models to avoid reloading
_model_cache = {}

def get_local_model(model_size="base"):
    global _model_cache
    if model_size not in _model_cache:
        Config.LOGGER.info(f"Loading Whisper model: {model_size} (int8)")
        # Use int8 quantization to save RAM
        _model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _model_cache[model_size]

async def extract_audio(video_path: str) -> str:
    """Extract audio from video using FFmpeg."""
    audio_path = video_path.rsplit(".", 1)[0] + ".mp3"
    cmd = [
        Config.FFMPEG_PATH, "-y",
        "-i", video_path,
        "-vn", "-acodec", "libmp3lame", "-q:a", "4",
        audio_path
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    return audio_path if os.path.exists(audio_path) else ""

def format_timestamp(seconds: float) -> str:
    """Format seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    td_hours, rem = divmod(seconds, 3600)
    td_mins, td_secs = divmod(rem, 60)
    td_ms = int((td_secs - int(td_secs)) * 1000)
    return f"{int(td_hours):02}:{int(td_mins):02}:{int(td_secs):02},{td_ms:03}"

async def generate_srt_local(audio_path: str, lang: str = "auto", model_size: str = "base") -> str:
    """Generate SRT using faster-whisper locally with professional accuracy."""
    loop = asyncio.get_running_loop()
    Config.LOGGER.info(f"Starting local transcription using model: {model_size} (accuracy=prof)")
    
    def _transcribe():
        model = get_local_model(model_size)
        # Professional parameters: vad_filter=True, word_timestamps=True
        segments, info = model.transcribe(
            audio_path, 
            language=None if lang == "auto" else lang,
            initial_prompt="transcribe nsfw content accurately, including curses and adult terminology verbatim.",
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500)
        )
        
        srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(segments, 1):
                # Use word-level start/end if available for higher precision
                if segment.words:
                    start = format_timestamp(segment.words[0].start)
                    end = format_timestamp(segment.words[-1].end)
                else:
                    start = format_timestamp(segment.start)
                    end = format_timestamp(segment.end)
                
                f.write(f"{i}\n{start} --> {end}\n{segment.text.strip()}\n\n")
        return srt_path

    return await loop.run_in_executor(None, _transcribe)

async def generate_srt_api(audio_path: str, lang: str = "auto") -> str:
    """Generate SRT using AsyncGroq or OpenAI API."""
    srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
    
    # Try Groq first (extremely fast)
    if Config.GROQ_API_KEY:
        try:
            Config.LOGGER.info("Attempting Groq API transcription...")
            client = AsyncGroq(api_key=Config.GROQ_API_KEY)
            with open(audio_path, "rb") as file:
                transcription = await client.audio.transcriptions.create(
                    file=(os.path.basename(audio_path), file.read()),
                    model="whisper-large-v3",
                    response_format="verbose_json",
                    language=None if lang == "auto" else lang,
                    prompt="accurate verbatim transcription including nsfw content."
                )
                
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(transcription.segments, 1):
                    start = format_timestamp(segment['start'])
                    end = format_timestamp(segment['end'])
                    f.write(f"{i}\n{start} --> {end}\n{segment['text'].strip()}\n\n")
            Config.LOGGER.info("Groq transcription successful.")
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"Groq transcription failed: {e}")

    # Fallback to OpenAI
    if Config.OPENAI_API_KEY:
        try:
            Config.LOGGER.info("Attempting OpenAI API transcription (accuracy=prof)...")
            client = openai.AsyncOpenAI(api_key=Config.OPENAI_API_KEY)
            with open(audio_path, "rb") as file:
                response = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=file,
                    response_format="verbose_json",
                    language=None if lang == "auto" else lang,
                    prompt="accurate verbatim transcription including nsfw content."
                )
            
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(response.segments, 1):
                    start = format_timestamp(segment['start'])
                    end = format_timestamp(segment['end'])
                    f.write(f"{i}\n{start} --> {end}\n{segment['text'].strip()}\n\n")
            
            Config.LOGGER.info("OpenAI transcription successful.")
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"OpenAI transcription failed: {e}")

    return ""

async def get_video_duration(video_path: str) -> float:
    """Get video duration using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    try:
        return float(stdout.decode().strip())
    except:
        return 0.0

async def burn_subtitles(video_path: str, srt_path: str, progress_callback=None) -> str:
    """Burn subtitles into video using FFmpeg."""
    output_path = video_path.rsplit(".", 1)[0] + "_subbed.mp4"
    duration = await get_video_duration(video_path)
    
    # FFmpeg command for burning subtitles
    # Note: on Linux, we need to escape commas and other special chars in the filename
    escaped_srt = srt_path.replace("'", "'\\''").replace(":", "\\:").replace(",", "\\,")
    cmd = [
        Config.FFMPEG_PATH, "-y",
        "-i", video_path,
        "-vf", f"subtitles='{escaped_srt}'",
        "-c:a", "copy",  # Copy audio to save time
        output_path
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    if progress_callback:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode().strip()
            # Extract time=HH:MM:SS.ms
            match = re.search(r"time=(\d+):(\d+):(\d+.\d+)", line_str)
            if match and duration > 0:
                h, m, s = map(float, match.groups())
                current_time = h * 3600 + m * 60 + s
                percent = min(100, int((current_time / duration) * 100))
                await progress_callback(percent)
    
    await process.wait()
    return output_path if os.path.exists(output_path) else ""

async def generate_subtitles(video_path: str, lang: str = "auto", method: str = "local", model: str = "base") -> str:
    """Main entry point for subtitle generation."""
    Config.LOGGER.info(f"Subtitle request: method={method}, model={model}, lang={lang}")
    audio_path = await extract_audio(video_path)
    if not audio_path:
        Config.LOGGER.error("Audio extraction failed.")
        return ""
    
    try:
        if method == "api" and (Config.GROQ_API_KEY or Config.OPENAI_API_KEY):
            srt_path = await generate_srt_api(audio_path, lang)
        else:
            srt_path = await generate_srt_local(audio_path, lang, model)
            
        if not srt_path:
            Config.LOGGER.warning(f"Transcription failed for method: {method}")
        return srt_path
    finally:
        # Cleanup audio file
        if os.path.exists(audio_path):
            os.remove(audio_path)
