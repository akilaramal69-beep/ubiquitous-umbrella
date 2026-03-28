import os
import asyncio
import time
import re
import json
import tempfile
from faster_whisper import WhisperModel
from plugins.config import Config
from groq import AsyncGroq
import openai
import stable_whisper

# Cache for local models to avoid reloading
_model_cache = {}

# Model size mapping for better accuracy selection
MODEL_SIZE_MAP = {
    "base": "base",
    "small": "small",
    "distil-large-v3": "large-v3",  # distil-large-v3 uses large-v3 architecture
    "medium": "medium",
    "large-v3": "large-v3",
}

# Language-specific prompts for improved accuracy
LANGUAGE_PROMPTS = {
    "en": "Professional transcription. Clear speech, proper punctuation, verbatim including names and technical terms.",
    "ja": "Japanese transcription. Include honorifics. Transliterate foreign words when appropriate.",
    "ko": "Korean transcription. Include particles and formality markers.",
    "zh": "Chinese transcription. Include tone markers for ambiguous characters when helpful.",
    "es": "Spanish transcription. Include regional accents and colloquialisms.",
    "fr": "French transcription. Include liaison and elision where spoken.",
    "de": "German transcription. Include compound words and case markers.",
    "ar": "Arabic transcription. Include diacritics where audible.",
    "hi": "Hindi transcription. Include code-mixing when present.",
    "pt": "Portuguese transcription. Include European and Brazilian variants.",
    "ru": "Russian transcription. Include proper names and technical terms.",
    "auto": "Professional verbatim transcription including all speech, names, and technical terms.",
}

# Domain-specific context hints
DOMAIN_PROMPTS = {
    "technical": "Technical content. Include programming terms, technical jargon, code snippets mentioned, and software names.",
    "medical": "Medical content. Include drug names, medical terminology, anatomical terms.",
    "legal": "Legal content. Include case names, statute references, legal terminology.",
    "academic": "Academic content. Include citations, research terms, methodology descriptions.",
    "general": "General conversation. Include all spoken words with proper punctuation.",
}


def get_progress_bar(percent: int, width: int = 15) -> str:
    """Generate a visual progress bar string."""
    percent = min(100, max(0, percent))
    filled = int(width * percent / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {percent}%"


def get_stable_model(model_size="base"):
    global _model_cache
    mapped_size = MODEL_SIZE_MAP.get(model_size, model_size)
    cache_key = f"stable_{mapped_size}"
    
    if cache_key not in _model_cache:
        Config.LOGGER.info(f"Loading Stable-Whisper model: {mapped_size} (int8)")
        try:
            _model_cache[cache_key] = stable_whisper.load_faster_whisper(
                mapped_size, device="cpu", compute_type="int8"
            )
        except Exception as e:
            Config.LOGGER.error(f"Failed to load {mapped_size}: {e}. Retrying with local_files_only...")
            _model_cache[cache_key] = stable_whisper.load_faster_whisper(
                mapped_size, device="cpu", compute_type="int8", local_files_only=True
            )
    return _model_cache[cache_key]


async def preprocess_audio(input_path: str, output_path: str, progress_callback=None) -> str:
    """Apply audio preprocessing for better transcription quality.
    
    Steps:
    1. Normalize volume to consistent level
    2. Remove background noise using simple high-pass filter
    3. Convert to optimal format for Whisper (16kHz mono WAV)
    """
    if progress_callback: await progress_callback(5)
    
    cmd = [
        Config.FFMPEG_PATH, "-y",
        "-i", input_path,
        # High-pass filter to remove low-frequency rumble
        "-af", "highpass=f=80,lowpass=f=8000,volume=2.0,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "16000",  # Optimal sample rate for Whisper
        "-ac", "1",      # Mono channel
        "-acodec", "pcm_s16le",  # Uncompressed 16-bit PCM for quality
        output_path
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    
    if progress_callback: await progress_callback(15)
    
    if process.returncode != 0:
        Config.LOGGER.warning(f"Audio preprocessing failed, using original: {stderr.decode()[:200]}")
        return input_path
    
    return output_path


async def extract_audio_optimized(video_path: str, progress_callback=None) -> str:
    """Extract and optimize audio from video for maximum transcription accuracy."""
    if progress_callback: await progress_callback(5)
    
    dir_name = os.path.dirname(video_path)
    video_basename = os.path.splitext(os.path.basename(video_path))[0]
    
    # Use WAV for maximum quality during processing
    raw_audio = os.path.join(dir_name, f"{video_basename}_raw.wav")
    processed_audio = os.path.join(dir_name, f"{video_basename}_audio.wav")
    final_audio = video_path.rsplit(".", 1)[0] + ".wav"
    
    import shutil
    
    try:
        # Step 1: Extract audio with high quality settings
        if progress_callback: await progress_callback(10)
        
        extract_cmd = [
            Config.FFMPEG_PATH, "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "pcm_s16le",  # Uncompressed for quality
            "-ar", "48000",  # High sample rate for processing
            "-ac", "2",
            "-af", "aresample=48000",
            raw_audio
        ]
        
        process = await asyncio.create_subprocess_exec(
            *extract_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await process.wait()
        
        if process.returncode != 0:
            Config.LOGGER.warning("High quality extraction failed, trying fallback...")
            # Fallback to simpler extraction
            extract_cmd[-3:] = ["-ar", "16000", "-ac", "1", raw_audio]
            process = await asyncio.create_subprocess_exec(
                *extract_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await process.wait()
        
        if progress_callback: await progress_callback(40)
        
        # Step 2: Preprocess audio (normalize, noise reduction)
        if os.path.exists(raw_audio):
            processed = await preprocess_audio(raw_audio, processed_audio, progress_callback)
            if processed == raw_audio:
                shutil.copy(raw_audio, processed_audio)
        else:
            # Direct extraction to processed format as fallback
            processed_audio = raw_audio
        
        if progress_callback: await progress_callback(80)
        
        # Step 3: Rename to final path
        if os.path.exists(final_audio):
            os.remove(final_audio)
        if processed_audio != final_audio:
            shutil.copy(processed_audio, final_audio)
        
        if progress_callback: await progress_callback(95)
        
        # Cleanup temp files
        for f in [raw_audio, processed_audio]:
            if os.path.exists(f) and f != final_audio:
                try: os.remove(f)
                except: pass
        
        return final_audio
        
    except Exception as e:
        Config.LOGGER.error(f"Audio extraction exception: {e}")
        # Fallback to simple extraction
        try:
            final_audio = video_path.rsplit(".", 1)[0] + ".wav"
            cmd = [
                Config.FFMPEG_PATH, "-y",
                "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1",
                final_audio
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await process.wait()
            if os.path.exists(final_audio):
                return final_audio
        except:
            pass
        return ""


async def detect_language(audio_path: str, model_size: str = "base") -> str:
    """Pre-detect language for better transcription accuracy."""
    try:
        loop = asyncio.get_running_loop()
        
        def _detect():
            try:
                mapped_size = MODEL_SIZE_MAP.get(model_size, model_size)
                # Use smaller model for quick language detection
                detect_model = stable_whisper.load_faster_whisper("tiny", device="cpu", compute_type="int8")
                result = detect_model.transcribe(audio_path, language=None, beam_size=1, vad_filter=False)
                
                # Get detected language from result
                if hasattr(result, 'language'):
                    return result.language
                elif isinstance(result, dict):
                    return result.get('language', 'en')
                return 'en'
            except Exception as e:
                Config.LOGGER.warning(f"Language detection failed: {e}")
                return 'en'
        
        lang = await loop.run_in_executor(None, _detect)
        Config.LOGGER.info(f"Detected language: {lang}")
        return lang
        
    except Exception as e:
        Config.LOGGER.warning(f"Language detection error: {e}")
        return 'en'


# Legacy function for backwards compatibility
async def extract_audio(video_path: str, progress_callback=None) -> str:
    """Legacy audio extraction - now uses optimized version."""
    return await extract_audio_optimized(video_path, progress_callback)

def format_timestamp(seconds: float) -> str:
    """Format seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    td_hours, rem = divmod(seconds, 3600)
    td_mins, td_secs = divmod(rem, 60)
    td_ms = int((td_secs - int(td_secs)) * 1000)
    return f"{int(td_hours):02}:{int(td_mins):02}:{int(td_secs):02},{td_ms:03}"


def format_timestamp_vtt(seconds: float) -> str:
    """Format seconds to WebVTT timestamp format (HH:MM:SS.mmm)."""
    td_hours, rem = divmod(seconds, 3600)
    td_mins, td_secs = divmod(rem, 60)
    td_ms = int((td_secs - int(td_secs)) * 1000)
    return f"{int(td_hours):02}:{int(td_mins):02}:{int(td_secs):02}.{td_ms:03}"


def clean_srt_text(text: str) -> str:
    """Clean and normalize SRT text for better readability."""
    if not text:
        return ""
    # Remove multiple spaces
    text = re.sub(r'\s+', ' ', text)
    # Fix common transcription artifacts
    text = re.sub(r'\bi\b', 'I', text)  # Fix lowercase 'i' to 'I'
    # Add proper spacing around punctuation
    text = re.sub(r'([.!?,;:])([^\s])', r'\1 \2', text)
    # Remove space before punctuation (except quotes)
    text = re.sub(r'\s+([.!?,;:])', r'\1', text)
    # Clean up speaker tags like [Speaker 1]
    text = re.sub(r'\[.*?\]', '', text)
    text = text.strip()
    return text

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

async def generate_srt_local(audio_path: str, lang: str = "auto", model_size: str = "base", progress_callback=None) -> str:
    """Generate SRT using stable-whisper with millisecond-perfect timing and improved accuracy."""
    loop = asyncio.get_running_loop()
    mapped_model = MODEL_SIZE_MAP.get(model_size, model_size)
    Config.LOGGER.info(f"Starting stable-ts transcription: model={mapped_model}")
    
    # Get language-specific prompt for better accuracy
    effective_lang = lang if lang != "auto" else "en"
    initial_prompt = LANGUAGE_PROMPTS.get(effective_lang, LANGUAGE_PROMPTS["auto"])
    
    def _transcribe():
        try:
            model = get_stable_model(model_size)
            srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
            
            # Helper for stable-whisper progress reporting
            def _p_callback(seek, total):
                if progress_callback and total > 0:
                    percent = int((seek / total) * 100)
                    asyncio.run_coroutine_threadsafe(progress_callback(percent), loop)

            # stable-ts with optimized settings for better accuracy
            result = model.transcribe_stable(
                audio_path,
                language=None if lang == "auto" else lang,
                initial_prompt=initial_prompt,
                vad=True,
                vad_parameters={"min_silence_duration_ms": 500},  # Better pause detection
                beam_size=5,
                best_of=5,  # More candidates for better accuracy
                condition_on_previous_text=True,
                compression_ratio_threshold=2.4,  # Skip highly repetitive segments
                log_prob_threshold=-1.0,  # Skip low-confidence segments
                progress_callback=_p_callback
            )
            
            # Convert to SRT with word-level timing
            result.to_srt_vtt(srt_path, word_level=False)
            
            # Post-process SRT for better quality
            _post_process_srt(srt_path)
            
            Config.LOGGER.info(f"Stable-ts complete: {srt_path}")
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"Stable-ts transcription error: {e}")
            return ""

    return await loop.run_in_executor(None, _transcribe)


def _post_process_srt(srt_path: str):
    """Post-process generated SRT for improved readability."""
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Clean up the SRT content
        lines = content.split("\n")
        cleaned_lines = []
        
        for line in lines:
            # Skip empty lines but preserve structure
            if line.strip():
                cleaned = clean_srt_text(line)
                cleaned_lines.append(cleaned)
            else:
                cleaned_lines.append(line)
        
        # Write back
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(cleaned_lines))
            
    except Exception as e:
        Config.LOGGER.warning(f"SRT post-processing failed: {e}")

async def generate_srt_api(audio_path: str, lang: str = "auto") -> str:
    """Generate SRT using AsyncGroq or OpenAI API with improved prompts."""
    srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
    
    # Get language-specific prompt
    effective_lang = lang if lang != "auto" else "en"
    prompt = LANGUAGE_PROMPTS.get(effective_lang, LANGUAGE_PROMPTS["auto"])
    
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
                    prompt=prompt
                )
                
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(transcription.segments, 1):
                    start = format_timestamp(segment['start'])
                    end = format_timestamp(segment['end'])
                    text = clean_srt_text(segment['text'].strip())
                    f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
            
            _post_process_srt(srt_path)
            Config.LOGGER.info("Groq transcription successful.")
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"Groq transcription failed: {e}")

    if Config.OPENAI_API_KEY:
        try:
            Config.LOGGER.info("Attempting OpenAI API transcription...")
            client = openai.AsyncOpenAI(api_key=Config.OPENAI_API_KEY)
            with open(audio_path, "rb") as file:
                response = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=file,
                    response_format="verbose_json",
                    language=None if lang == "auto" else lang,
                    prompt=prompt
                )
            
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(response.segments, 1):
                    start = format_timestamp(segment['start'])
                    end = format_timestamp(segment['end'])
                    text = clean_srt_text(segment['text'].strip())
                    f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
            
            _post_process_srt(srt_path)
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
            import torchaudio
            import gc
            
            device = "cpu"
            compute_type = "int8"
            srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
            mapped_model = MODEL_SIZE_MAP.get(model_size, model_size)
            
            # 1. Load and preprocess audio
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(5), loop)
            waveform, sample_rate = torchaudio.load(audio_path)
            
            # WhisperX expects 16kHz mono audio (numpy array)
            if sample_rate != 16000:
                resampler = torchaudio.transforms.Resample(sample_rate, 16000)
                waveform = resampler(waveform)
            
            # Convert to mono if stereo
            if waveform.shape[0] > 1:
                waveform = torch.mean(waveform, dim=0, keepdim=True)
            
            audio_numpy = waveform.squeeze().numpy()
            
            # 2. Transcribe with improved settings
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(20), loop)
            
            # Use large-v3 equivalent for distil-large-v3
            whisperx_model = mapped_model
            model = whisperx.load_model(whisperx_model, device, compute_type=compute_type)
            
            # Better transcription settings
            result = model.transcribe(
                audio_numpy, 
                batch_size=16,
                language=None if lang == "auto" else lang,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500}
            )
            
            del model
            gc.collect()
            
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(60), loop)
            
            # 3. Align whisper output with improved settings
            language_code = result.get("language", lang if lang != "auto" else "en")
            model_a, metadata = whisperx.load_align_model(language_code=language_code, device=device)
            
            result = whisperx.align(
                result["segments"], 
                model_a, 
                metadata, 
                audio_numpy, 
                device, 
                return_char_alignments=False
            )
            
            del model_a
            gc.collect()
            
            if progress_callback: asyncio.run_coroutine_threadsafe(progress_callback(90), loop)
            
            # 4. Export to SRT with post-processing
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(result["segments"], 1):
                    start = format_timestamp(segment['start'])
                    end = format_timestamp(segment['end'])
                    text = clean_srt_text(segment['text'].strip())
                    f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
            
            _post_process_srt(srt_path)
            Config.LOGGER.info(f"WhisperX complete: {srt_path}")
            return srt_path
        except Exception as e:
            Config.LOGGER.error(f"WhisperX error: {e}")
            return ""

    return await loop.run_in_executor(None, _transcribe)


async def generate_subtitles(video_path: str, lang: str = "auto", method: str = "local", model: str = "base", engine: str = "stable-ts", progress_callback=None) -> str:
    """Main entry point for subtitle generation with optimized accuracy."""
    Config.LOGGER.info(f"Subtitle request: method={method}, model={model}, engine={engine}, lang={lang}")
    
    # Progress helper for audio extraction (0-15%)
    async def extraction_p_cb(p):
        if progress_callback:
            await progress_callback(int(p * 0.15))

    # Use optimized audio extraction for better accuracy
    audio_path = await extract_audio_optimized(video_path, progress_callback=extraction_p_cb)
    if not audio_path:
        return ""
    
    try:
        if method == "api" and (Config.GROQ_API_KEY or Config.OPENAI_API_KEY):
            return await generate_srt_api(audio_path, lang)
        
        # Shift progress for transcription stage (15% to 100%)
        async def transcription_p_cb(p):
            if progress_callback:
                shifted_p = 15 + int(p * 0.85)
                await progress_callback(min(100, shifted_p))
        
        if engine == "whisperx":
            return await generate_srt_whisperx(audio_path, lang, model, progress_callback=transcription_p_cb)
        else:
            return await generate_srt_local(audio_path, lang, model, progress_callback=transcription_p_cb)
            
    finally:
        # Cleanup temp audio files
        if audio_path and os.path.exists(audio_path):
            try: os.remove(audio_path)
            except: pass
