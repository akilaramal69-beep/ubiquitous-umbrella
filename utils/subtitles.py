import os
import asyncio
import time
import re
import json
from faster_whisper import WhisperModel
from plugins.config import Config
from groq import AsyncGroq
import openai

# Cache for local models to avoid reloading
_model_cache = {}

def get_local_model(model_size="base"):
    global _model_cache
    if model_size not in _model_cache:
        Config.LOGGER.info(f"Loading Whisper model: {model_size} (int8)")
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
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    return audio_path if os.path.exists(audio_path) else ""

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

async def generate_srt_local(audio_path: str, lang: str = "auto", model_size: str = "base") -> str:
    """Generate SRT using faster-whisper locally with professional accuracy and robust fallbacks."""
    loop = asyncio.get_running_loop()
    Config.LOGGER.info(f"Starting local transcription: model={model_size}, accuracy=ultra")
    
    def _transcribe():
        try:
            model = get_local_model(model_size)
            # Professional parameters
            segments_gen, info = model.transcribe(
                audio_path, 
                language=None if lang == "auto" else lang,
                initial_prompt="transcribe nsfw content accurately, including curses and adult terminology verbatim.",
                word_timestamps=True,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                beam_size=5
            )
            
            # Consume generator into a list
            segments = list(segments_gen)
            Config.LOGGER.info(f"Transcription complete: found {len(segments)} segments.")
            
            srt_path = audio_path.rsplit(".", 1)[0] + ".srt"
            
            # Try word-based re-segmentation first
            all_words = []
            for s in segments:
                if s.words:
                    all_words.extend(s.words)
            
            Config.LOGGER.info(f"Word-level data: {len(all_words)} words found.")
            
            if all_words:
                # Group words into professional-sized segments
                refined_segments = []
                current_segment = []
                max_duration = 4.0 # seconds
                max_words = 12
                max_gap = 1.0     # seconds
                
                for word in all_words:
                    if not current_segment:
                        current_segment.append(word)
                        continue
                    
                    duration = word.end - current_segment[0].start
                    gap = word.start - current_segment[-1].end
                    
                    if (len(current_segment) >= max_words or 
                        duration > max_duration or 
                        gap > max_gap or 
                        current_segment[-1].word.strip().endswith(('.', '?', '!'))):
                        
                        refined_segments.append(current_segment)
                        current_segment = [word]
                    else:
                        current_segment.append(word)
                
                if current_segment:
                    refined_segments.append(current_segment)

                with open(srt_path, "w", encoding="utf-8") as f:
                    for i, group in enumerate(refined_segments, 1):
                        start = format_timestamp(group[0].start)
                        end = format_timestamp(group[-1].end)
                        text = "".join([w.word for w in group]).strip()
                        f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
                
                return srt_path
            
            # Fallback: If no word data, use standard segments
            elif segments:
                Config.LOGGER.warning("No word-level data found. Falling back to standard segments.")
                with open(srt_path, "w", encoding="utf-8") as f:
                    for i, segment in enumerate(segments, 1):
                        start = format_timestamp(segment.start)
                        end = format_timestamp(segment.end)
                        f.write(f"{i}\n{start} --> {end}\n{segment.text.strip()}\n\n")
                return srt_path
            
            else:
                Config.LOGGER.error("No transcription data produced.")
                return ""
        except Exception as e:
            Config.LOGGER.error(f"Local transcription error: {e}")
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
            
        return srt_path
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)
