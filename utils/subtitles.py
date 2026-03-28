import os
import asyncio
import time
import re
import json
from faster_whisper import WhisperModel
from plugins.config import Config
from groq import AsyncGroq
import openai
import stable_whisper

# Cache for local models to avoid reloading
_model_cache = {}

def get_progress_bar(percent: int, width: int = 15) -> str:
    """Generate a visual progress bar string."""
    percent = min(100, max(0, percent))
    filled = int(width * percent / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {percent}%"

def get_stable_model(model_size="base"):
    global _model_cache
    if f"stable_{model_size}" not in _model_cache:
        Config.LOGGER.info(f"Loading Stable-Whisper model: {model_size} (int8)")
        # stable_whisper wraps faster_whisper for best performance
        _model_cache[f"stable_{model_size}"] = stable_whisper.load_faster_whisper(
            model_size, device="cpu", compute_type="int8"
        )
    return _model_cache[f"stable_{model_size}"]

async def extract_audio(video_path: str, progress_callback=None) -> str:
    """Extract audio from video using FFmpeg with Clean Path strategy and progress reporting."""
    if progress_callback: await progress_callback(10) # Extraction is usually fast
    
    dir_name = os.path.dirname(video_path)
    clean_video = os.path.join(dir_name, "v_audio.mp4")
    audio_path = os.path.join(dir_name, "a.mp3")
    
    import shutil
    try:
        if os.path.exists(clean_video): os.remove(clean_video)
        if os.path.exists(audio_path): os.remove(audio_path)
        shutil.copy(video_path, clean_video)
        
        cmd = [
            Config.FFMPEG_PATH, "-y",
            "-i", "v_audio.mp4",
            "-vn", "-acodec", "libmp3lame", "-q:a", "4",
            "a.mp3"
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=dir_name
        )
        if progress_callback: await progress_callback(50)
        await process.wait()
        
        if progress_callback: await progress_callback(90)
        
        final_audio = video_path.rsplit(".", 1)[0] + ".mp3"
        if os.path.exists(final_audio): os.remove(final_audio)
        os.rename(audio_path, final_audio)
        return final_audio
    except Exception as e:
        Config.LOGGER.error(f"Audio extraction exception: {e}")
        return ""
    finally:
        if os.path.exists(clean_video): 
            try: os.remove(clean_video)
            except: pass

def format_timestamp(seconds: float) -> str:
    """Format seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    td_hours, rem = divmod(seconds, 3600)
    td_mins, td_secs = divmod(rem, 60)
    td_ms = int((td_secs - int(td_secs)) * 1000)
    return f"{int(td_hours):02}:{int(td_mins):02}:{int(td_secs):02},{td_ms:03}"

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

async def burn_subtitles_ffmpeg(video_path: str, srt_path: str, progress_callback=None) -> str:
    """Burn subtitles using FFmpeg with a 'Clean Path' strategy to avoid filter parsing errors."""
    if not os.path.exists(srt_path) or os.path.getsize(srt_path) < 10:
        Config.LOGGER.error(f"SRT file too small or missing: {srt_path}")
        return ""

    dir_name = os.path.dirname(video_path)
    # Use extremely simple names to bypass all FFmpeg filter escaping issues
    clean_video = os.path.join(dir_name, "v.mp4")
    clean_srt = os.path.join(dir_name, "s.srt")
    clean_output = os.path.join(dir_name, "o.mp4")
    
    # Backup original names if they exist at destination (unlikely with unique IDs)
    import shutil
    has_renamed = False
    try:
        if os.path.exists(clean_video): os.remove(clean_video)
        if os.path.exists(clean_srt): os.remove(clean_srt)
        if os.path.exists(clean_output): os.remove(clean_output)
        
        shutil.copy(video_path, clean_video)
        shutil.copy(srt_path, clean_srt)
        has_renamed = True
        
        duration = await get_video_duration(clean_video)
        
        # Now the path is very simple: "s.srt". No colons, no commas, no special chars!
        cmd = [
            Config.FFMPEG_PATH, "-y",
            "-i", clean_video,
            "-vf", "subtitles=s.srt:force_style='Alignment=2,Outline=1,BorderStyle=1,Fontsize=18'",
            "-c:a", "copy",
            "-c:v", "libx264", "-preset", "superfast", "-crf", "23",
            clean_output
        ]
        
        # Execute in the same directory so relative paths work perfectly for 'subtitles=s.srt'
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=dir_name
        )

        full_output = []
        if progress_callback:
            while True:
                line = await process.stdout.readline()
                if not line: break
                line_str = line.decode().strip()
                full_output.append(line_str)
                match = re.search(r"time=(\d+):(\d+):(\d+.\d+)", line_str)
                if match and duration > 0:
                    h, m, s = map(float, match.groups())
                    current_time = h * 3600 + m * 60 + s
                    percent = min(100, int((current_time / duration) * 100))
                    await progress_callback(percent)
        
        await process.wait()
        
        if os.path.exists(clean_output) and os.path.getsize(clean_output) > os.path.getsize(video_path) * 0.5:
            # Move result to the expected final path
            final_output = video_path.rsplit(".", 1)[0] + "_sub_ff.mp4"
            if os.path.exists(final_output): os.remove(final_output)
            os.rename(clean_output, final_output)
            return final_output
        
        error_log = "\n".join(full_output[-15:])
        Config.LOGGER.error(f"FFmpeg burning failed. Stderr snippet:\n{error_log}")
        return ""
        
    except Exception as e:
        Config.LOGGER.error(f"Clean Path burning failed: {e}")
        return ""
    finally:
        # Cleanup clean files
        if has_renamed:
            for f in [clean_video, clean_srt, clean_output]:
                if os.path.exists(f): 
                    try: os.remove(f)
                    except: pass

async def burn_subtitles_moviepy(video_path: str, srt_path: str, progress_callback=None) -> str:
    """Burn subtitles using MoviePy (fallback method)."""
    try:
        Config.LOGGER.info("Attempting MoviePy subtitle burning fallback...")
        try:
            from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip
            from moviepy.video.tools.subtitles import SubtitlesClip
        except ImportError:
            # MoviePy v2.0+ compatibility
            from moviepy.video.io.VideoFileClip import VideoFileClip
            from moviepy.video.VideoClip import TextClip
            from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip
            from moviepy.video.tools.subtitles import SubtitlesClip
        
        import pysrt

        output_path = video_path.rsplit(".", 1)[0] + "_sub_mp.mp4"
        
        def generator(txt):
            # Generic font 'Sans' or None to avoid ImageMagick errors
            return TextClip(txt, font='Sans', fontsize=24, color='white', 
                            stroke_color='black', stroke_width=1, method='caption', size=(640, None))

        video = VideoFileClip(video_path)
        subtitles = SubtitlesClip(srt_path, generator)
        
        result = CompositeVideoClip([video, subtitles.set_pos(('center', 'bottom'))])
        
        # MoviePy write_videofile is sync, run in executor
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: result.write_videofile(
            output_path, codec='libx264', audio_codec='aac', temp_audiofile='temp-audio.m4a', 
            remove_temp=True, logger=None, threads=2
        ))
        
        video.close()
        return output_path if os.path.exists(output_path) else ""
    except Exception as e:
        Config.LOGGER.error(f"MoviePy burning failed: {e}")
        return ""

async def burn_subtitles(video_path: str, srt_path: str, progress_callback=None) -> str:
    """Main entry point for burning subtitles with dual-method fallback."""
    if not os.path.exists(srt_path) or os.path.getsize(srt_path) < 10:
        Config.LOGGER.error("SRT file missing or empty. Skipping burn.")
        return ""

    # 1. Try FFmpeg
    Config.LOGGER.info("Starting FFmpeg subtitle burning...")
    burned_ff = await burn_subtitles_ffmpeg(video_path, srt_path, progress_callback)
    if burned_ff:
        Config.LOGGER.info("FFmpeg burning successful.")
        return burned_ff
    
    # 2. Fallback to MoviePy
    Config.LOGGER.warning("FFmpeg burning failed, falling back to MoviePy...")
    if progress_callback:
        await progress_callback("MoviePy processing...")
    
    burned_mp = await burn_subtitles_moviepy(video_path, srt_path)
    if burned_mp:
        Config.LOGGER.info("MoviePy burning successful.")
        return burned_mp
    
    Config.LOGGER.error("All subtitle burning methods failed.")
    return ""

def get_stable_model(model_size="base"):
    global _model_cache
    if f"stable_{model_size}" not in _model_cache:
        Config.LOGGER.info(f"Loading Stable-Whisper model: {model_size} (int8)")
        try:
            # stable_whisper wraps faster_whisper for best performance
            _model_cache[f"stable_{model_size}"] = stable_whisper.load_faster_whisper(
                model_size, device="cpu", compute_type="int8"
            )
        except Exception as e:
            Config.LOGGER.error(f"First attempt to load model failed: {e}. Trying with local_files_only=True")
            # If first attempt fails (e.g. 'Authorization' error), try loading from local cache
            _model_cache[f"stable_{model_size}"] = stable_whisper.load_faster_whisper(
                model_size, device="cpu", compute_type="int8", local_files_only=True
            )
    return _model_cache[f"stable_{model_size}"]

async def generate_srt_local(audio_path: str, lang: str = "auto", model_size: str = "base", progress_callback=None) -> str:
    """Generate SRT using stable-whisper with millisecond-perfect timing and progress reporting."""
    loop = asyncio.get_running_loop()
    Config.LOGGER.info(f"Starting stable-ts transcription: model={model_size}")
    
    def _transcribe():
        try:
            model = get_stable_model(model_size)
            srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
            
            # Helper for stable-whisper progress reporting
            def _p_callback(seek, total):
                if progress_callback and total > 0:
                    percent = int((seek / total) * 100)
                    # Use run_coroutine_threadsafe to call async callback from transcription thread
                    asyncio.run_coroutine_threadsafe(progress_callback(percent), loop)

            # stable-ts handles VAD and word-level stabilization internally
            result = model.transcribe_stable(
                audio_path,
                language=None if lang == "auto" else lang,
                initial_prompt="Transcribe verbatim, including all profanity and slang. Perfect sync.",
                vad=True,
                beam_size=5,
                condition_on_previous_text=True,
                progress_callback=_p_callback
            )
            
            # Professional re-segmentation built-into stable-ts
            result.to_srt_vtt(srt_path, word_level=False)
            
            Config.LOGGER.info(f"Stable-ts complete: {srt_path}")
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"Stable-ts transcription error: {e}")
            return ""

    return await loop.run_in_executor(None, _transcribe)

async def generate_srt_api(audio_path: str, lang: str = "auto") -> str:
    """Generate SRT using AsyncGroq or OpenAI API."""
    srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
    
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

async def generate_srt_whisperx(audio_path: str, lang: str = "auto", model_size: str = "base", progress_callback=None) -> str:
    """Generate SRT using WhisperX with in-memory audio loading for speed/efficiency."""
    loop = asyncio.get_running_loop()
    Config.LOGGER.info(f"Starting WhisperX in-memory transcription: model={model_size}")
    
    def _transcribe():
        try:
            import whisperx
            import torch
            import gc
            import numpy as np
            import subprocess
            
            device = "cpu"
            compute_type = "int8"
            srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
            
            # 1. Load audio in-memory (Multi-stage fallback)
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(5), loop)
            
            waveform = None
            sample_rate = 16000
            
            # STAGE A: TorchCodec (Fastest, if environment is correct)
            try:
                import torchcodec
                Config.LOGGER.info("Attempting TorchCodec decoding...")
                decoder = torchcodec.decoders.AudioDecoder(audio_path)
                waveform = decoder.get_whole_audio()
                sample_rate = int(decoder.metadata.sample_rate)
                if waveform.ndim == 2 and waveform.shape[1] < waveform.shape[0]:
                    waveform = waveform.T
            except Exception as te:
                Config.LOGGER.warning(f"TorchCodec failed: {te}")
                
                # STAGE B: Torchaudio (Standard)
                try:
                    import torchaudio
                    Config.LOGGER.info("Attempting Torchaudio decoding...")
                    waveform, sample_rate = torchaudio.load(audio_path)
                except Exception as ae:
                    Config.LOGGER.warning(f"Torchaudio failed: {ae}")
                    
                    # STAGE C: FFmpeg Binary (Ultimate Robustness)
                    Config.LOGGER.info("Using robust FFmpeg-to-Numpy fallback.")
                    try:
                        cmd = [
                            'ffmpeg', '-i', audio_path,
                            '-f', 'f32le', '-acodec', 'pcm_f32le',
                            '-ac', '1', '-ar', '16000', '-'
                        ]
                        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        out, err = process.communicate()
                        if process.returncode != 0:
                            raise Exception(f"FFmpeg error: {err.decode()}")
                            
                        audio_numpy = np.frombuffer(out, dtype=np.float32)
                        waveform = torch.from_numpy(audio_numpy).unsqueeze(0)
                        sample_rate = 16000
                    except Exception as fe:
                        Config.LOGGER.error(f"FAIL: All audio backends failed! {fe}")
                        return ""

            # 2. Resample & Mono-convert in-memory (only if not already 16k mono)
            if sample_rate != 16000 or waveform.shape[0] > 1:
                import torchaudio
                if sample_rate != 16000:
                    resampler = torchaudio.transforms.Resample(sample_rate, 16000)
                    waveform = resampler(waveform)
                if waveform.shape[0] > 1:
                    waveform = torch.mean(waveform, dim=0, keepdim=True)
            
            # Set to final mono/16k state
            audio_in_memory = {
                'waveform': waveform, # (channel, time) torch.Tensor
                'sample_rate': 16000
            }
            
            audio_numpy = audio_in_memory['waveform'].squeeze().numpy()
            
            # 3. Transcribe
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(20), loop)
            model = whisperx.load_model(model_size, device, compute_type=compute_type)
            result = model.transcribe(audio_numpy, batch_size=8)
            
            gc.collect()
            
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(60), loop)
            
            # 4. Align whisper output
            language_code = result["language"]
            model_a, metadata = whisperx.load_align_model(language_code=language_code, device=device)
            result = whisperx.align(result["segments"], model_a, metadata, audio_numpy, device, return_char_alignments=False)
            
            del model_a
            gc.collect()
            
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(90), loop)
            
            # 5. Export to SRT
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(result["segments"], 1):
                    start = format_timestamp(segment['start'])
                    end = format_timestamp(segment['end'])
                    text = segment['text'].strip()
                    f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
            
            Config.LOGGER.info(f"WhisperX in-memory complete: {srt_path}")
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"WhisperX critical error: {e}")
            return ""

    return await loop.run_in_executor(None, _transcribe)

async def generate_subtitles(video_path: str, lang: str = "auto", method: str = "local", model: str = "base", engine: str = "stable-ts", progress_callback=None) -> str:
    """Main entry point for subtitle generation with engine selection."""
    Config.LOGGER.info(f"Subtitle request: method={method}, model={model}, engine={engine}, lang={lang}")
    
    # Progress helper for audio extraction (0-10%)
    async def extraction_p_cb(p):
        if progress_callback:
            await progress_callback(int(p * 0.1))

    audio_path = await extract_audio(video_path, progress_callback=extraction_p_cb)
    if not audio_path:
        return ""
    
    try:
        if method == "api" and (Config.GROQ_API_KEY or Config.OPENAI_API_KEY):
            return await generate_srt_api(audio_path, lang)
        
        # Shift progress for transcription stage (10% to 100%)
        async def transcription_p_cb(p):
            if progress_callback:
                shifted_p = 10 + int(p * 0.9)
                await progress_callback(min(100, shifted_p))
        
        if engine == "whisperx":
            return await generate_srt_whisperx(audio_path, lang, model, progress_callback=transcription_p_cb)
        else:
            return await generate_srt_local(audio_path, lang, model, progress_callback=transcription_p_cb)
            
    finally:
        if os.path.exists(audio_path):
            try: os.remove(audio_path)
            except: pass
