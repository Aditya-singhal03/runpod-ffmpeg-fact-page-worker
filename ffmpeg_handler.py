# ffmpeg_handler.py
import runpod
import subprocess
import os
import base64
import tempfile
import json
import requests

def download_file(url, local_filename):
    """Downloads a file from a URL to a local path."""
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(local_filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"Downloaded {url} to {local_filename}")
            return True
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {url}: {e}")
        return False

async def handler(job):
    """
    Main handler for the FFmpeg video creation worker.
    """
    job_input = job['input']
    print(f"Job Input Received: {json.dumps(job_input, indent=2)}")

    with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
        # --- 1. Download and Prepare Inputs ---
        
        # Download input videos
        input_video_paths = []
        for i, video_url in enumerate(job_input.get("video_urls", [])):
            video_path = os.path.join(temp_dir, f"video_{i}.mp4")
            if not download_file(video_url, video_path):
                return {"error": f"Failed to download video from {video_url}"}
            input_video_paths.append(video_path)

        # Decode narration audio from base64
        narration_audio_path = os.path.join(temp_dir, "narration.wav")
        audio_base64 = job_input.get("narration_audio_base64")
        if not audio_base64:
            return {"error": "Missing narration_audio_base64"}
        with open(narration_audio_path, "wb") as f:
            f.write(base64.b64decode(audio_base64))
        
        # Get caption data
        caption_data = job_input.get("caption_data", {})
        words_data = caption_data.get("words", [])

        # --- 2. Build the Monster FFmpeg Command ---
        
        # Start with all video and audio inputs
        ffmpeg_cmd = ['ffmpeg', '-y']
        for path in input_video_paths:
            ffmpeg_cmd.extend(['-i', path])
        ffmpeg_cmd.extend(['-i', narration_audio_path])
        
        # --- 2a. Build the Filter Complex String ---
        filter_complex = []

        # Stitch video streams together
        video_streams = "".join([f"[{i}:v]" for i in range(len(input_video_paths))])
        filter_complex.append(f"{video_streams}concat=n={len(input_video_paths)}:v=1:a=0[stitched_v]")

        # Format the stitched video for 9:16 Reels aspect ratio
        # This creates a professional-looking blurred background effect
        filter_complex.append(
            "[stitched_v]split=2[v_main][v_bg];"
            "[v_bg]scale=1080:1920,boxblur=20:1[bg];"
            "[v_main]scale=-1:1080[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[formatted_v]"
        )

        # Generate the drawtext filters for animated captions
        drawtext_filters = []
        for word_info in words_data:
            text = word_info['text'].strip().replace("'", r"\'").replace(":", r"\:")
            start = word_info['start']
            end = word_info['end']
            
            drawtext_filters.append(
                "drawtext="
                "fontfile=/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf:" # A common, robust font
                f"text='{text}':"
                "fontcolor=white:fontsize=96:borderw=3:bordercolor=black:"
                "x=(w-text_w)/2:"  # Center horizontally
                "y=h*0.75:"        # Position vertically at 75% from the top
                f"enable='between(t,{start},{end})'"
            )
        
        # Add the drawtext filters to the main filter chain
        if drawtext_filters:
            filter_complex.append(f"[formatted_v]{','.join(drawtext_filters)}[final_v]")
            video_map = "[final_v]"
        else:
            video_map = "[formatted_v]" # No captions, just use the formatted video

        # --- 2b. Finalize the Command ---
        ffmpeg_cmd.extend(['-filter_complex', ";".join(filter_complex)])
        
        # Map the final video stream and the narration audio stream
        audio_input_index = len(input_video_paths) # Audio is the last input
        ffmpeg_cmd.extend(['-map', video_map])
        ffmpeg_cmd.extend(['-map', f'{audio_input_index}:a'])
        
        # Set output codecs and options. Use -shortest to trim video to audio length.
        output_video_path = os.path.join(temp_dir, "final_video.mp4")
        ffmpeg_cmd.extend([
            '-c:v', 'libx264',
            '-profile:v', 'main',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-pix_fmt', 'yuv420p',
            '-shortest', # This is KEY: Ends the video when the shortest stream (our audio) ends
            output_video_path
        ])

        # --- 3. Execute FFmpeg ---
        print(f"Executing FFmpeg command: {' '.join(ffmpeg_cmd)}")
        try:
            result = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
            print("FFmpeg execution successful.")
            print("FFmpeg STDERR:", result.stderr)
        except subprocess.CalledProcessError as e:
            print("FFmpeg execution failed.")
            print("FFmpeg STDERR:", e.stderr)
            return {"error": "FFmpeg processing failed.", "details": e.stderr}

        # --- 4. Return the Final Video ---
        with open(output_video_path, "rb") as f:
            video_data = f.read()
        
        base64_video = base64.b64encode(video_data).decode('utf-8')
        
        return {
            "video_base64": base64_video,
            "filename": os.path.basename(output_video_path)
        }

runpod.serverless.start({"handler": handler})