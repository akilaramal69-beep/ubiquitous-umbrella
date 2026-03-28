import os
import asyncio
import time
from faster_whisper import WhisperModel
from plugins.config import Config
from groq import Groq
import openai

# Cache for local model to avoid reloading
_model_cache = {}

def get_local_model(model_size="base"):
    global _model_cache
    if model_size not in _model_cache:
        # Use int8 quantization to save RAM (critical for 1GB VPS)
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

async def generate_srt_local(audio_path: str, lang: str = "auto") -> str:
    """Generate SRT using faster-whisper locally."""
    loop = asyncio.get_running_loop()
    
    def _transcribe():
        model = get_local_model("base")
        # initial_prompt helps with NSFW/slang accuracy
        segments, info = model.transcribe(
            audio_path, 
            language=None if lang == "auto" else lang,
            initial_prompt="transcribe nsfw content accurately, including curses and adult terminology verbatim."
        )
        
        srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(segments, 1):
                start = format_timestamp(segment.start)
                end = format_timestamp(segment.end)
                f.write(f"{i}\n{start} --> {end}\n{segment.text.strip()}\n\n")
        return srt_path

    return await loop.run_in_executor(None, _transcribe)

async def generate_srt_api(audio_path: str, lang: str = "auto") -> str:
    """Generate SRT using Groq or OpenAI API."""
    srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
    
    # Try Groq first (faster and often free)
    if Config.GROQ_API_KEY:
        try:
            client = Groq(api_key=Config.GROQ_API_KEY)
            with open(audio_path, "rb") as file:
                transcription = client.audio.transcriptions.create(
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
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"Groq transcription failed: {e}")

    # Fallback to OpenAI
    if Config.OPENAI_API_KEY:
        try:
            client = openai.AsyncOpenAI(api_key=Config.OPENAI_API_KEY)
            with open(audio_path, "rb") as file:
                response = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=file,
                    response_format="srt",
                    language=None if lang == "auto" else lang,
                    prompt="accurate verbatim transcription including nsfw content."
                )
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(response)
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"OpenAI transcription failed: {e}")

    return ""

async def generate_subtitles(video_path: str, lang: str = "auto", method: str = "local") -> str:
    """Main entry point for subtitle generation."""
    audio_path = await extract_audio(video_path)
    if not audio_path:
        return ""
    
    try:
        if method == "api" and (Config.GROQ_API_KEY or Config.OPENAI_API_KEY):
            srt_path = await generate_srt_api(audio_path, lang)
        else:
            srt_path = await generate_srt_local(audio_path, lang)
            
        return srt_path
    finally:
        # Cleanup audio file
        if os.path.exists(audio_path):
            os.remove(audio_path)
