import runpod
import subprocess
import os
import base64
import tempfile
import json
import requests
from urllib.parse import quote
import boto3

def upload_to_r2(file_path):
    """Uploads a file to a Cloudflare R2 bucket and returns the public URL."""
    
    # 1. Get R2 credentials and bucket info from environment variables.
    try:
        account_id = os.environ['R2_ACCOUNT_ID']
        access_key_id = os.environ['R2_ACCESS_KEY_ID']
        secret_access_key = os.environ['R2_SECRET_ACCESS_KEY']
        bucket_name = os.environ['R2_BUCKET_NAME']
        public_url_base = os.environ['R2_PUBLIC_URL']
    except KeyError as e:
        print(f"ERROR: Missing required R2 environment variable: {e}")
        return None

    # 2. Construct the R2 endpoint URL.
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    
    # 3. Create an S3 client configured for R2.
    s3_client = boto3.client(
        's3',
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name='apac'
    )
    
    object_name = os.path.basename(file_path)

    try:
        print(f"Uploading {object_name} to R2 bucket {bucket_name}...")
        
        # 4. Upload the file.
        s3_client.upload_file(
            file_path,
            bucket_name,
            object_name,
            ExtraArgs={'ContentType': 'video/mp4'}
        )
        
        # 5. Construct the final public URL.
        final_url = f"{public_url_base.rstrip('/')}/{quote(object_name)}"
        
        print(f"File uploaded successfully. Public URL: {final_url}")
        return final_url

    except Exception as e:
        print(f"An unexpected error occurred during R2 upload: {e}")
        return None

def ffmpeg_escape(text):
    """Cleans text for use in an FFmpeg drawtext filter's text option."""
    text = str(text)
    return text.replace("'", "").replace('"', '')

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

def upload_to_gofile(file_path):
    """Uploads a file to GoFile.io and returns the download link."""
    try:
        # 1. Get a server to upload to
        server_response = requests.get("https://api.gofile.io/servers")
        server_response.raise_for_status()
        server = server_response.json()["data"]["servers"][0]["name"]
        print(f"GoFile server selected: {server}")

        # 2. Upload the file
        with open(file_path, 'rb') as f:
            files = {'file': f}
            upload_response = requests.post(f"https://{server}.gofile.io/uploadFile", files=files)
        
        upload_response.raise_for_status()
        upload_data = upload_response.json()["data"]
        download_link = upload_data["downloadPage"]
        print(f"File uploaded successfully. Download link: {download_link}")
        
        return download_link

    except requests.exceptions.RequestException as e:
        print(f"Error uploading to GoFile.io: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during upload: {e}")
        return None

def get_audio_duration(audio_path):
    """Get the duration of an audio file in seconds."""
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', audio_path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        return float(data['format']['duration'])
    except Exception as e:
        print(f"Error getting audio duration: {e}")
        return None

async def handler(job):
    job_input = job['input']
    print(f"Job Input Received: {job['id']}")

    with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
        # --- 1. Download and Prepare Inputs ---
        input_video_paths = []
        for i, video_url in enumerate(job_input.get("video_urls", [])):
            video_path = os.path.join(temp_dir, f"video_{i}.mp4")
            if not download_file(video_url, video_path):
                return {"error": f"Failed to download video from {video_url}"}
            input_video_paths.append(video_path)

        narration_audio_path = os.path.join(temp_dir, "narration.wav")
        audio_base64 = job_input.get("narration_audio_base64")
        if not audio_base64: 
            return {"error": "Missing narration_audio_base64"}
        with open(narration_audio_path, "wb") as f:
            f.write(base64.b64decode(audio_base64))
        
        caption_data = job_input.get("caption_data", {})
        words_data = caption_data.get("words", [])

        background_music_path = None
        if job_input.get("background_music_url"):
            background_music_path = os.path.join(temp_dir, "background_music.mp3")
            if not download_file(job_input["background_music_url"], background_music_path):
                return {"error": f"Failed to download background music from {job_input['background_music_url']}"}

        # --- 2. Get narration duration for video processing ---
        narration_duration = get_audio_duration(narration_audio_path)
        if not narration_duration:
            return {"error": "Could not determine narration audio duration"}
        
        print(f"Narration duration: {narration_duration} seconds")

        # --- 3. Create concat file for faster video processing ---
        concat_file_path = os.path.join(temp_dir, "concat_list.txt")
        with open(concat_file_path, 'w') as f:
            for video_path in input_video_paths:
                f.write(f"file '{video_path}'\n")

        # --- 4. Process videos in two fast steps ---
        
        # Step 1: Concatenate all videos quickly
        intermediate_video = os.path.join(temp_dir, "concatenated.mp4")
        concat_cmd = [
            'ffmpeg', '-y', '-threads', '0',
            '-f', 'concat', '-safe', '0', '-i', concat_file_path,
            '-c', 'copy',  # Copy streams without re-encoding for speed
            intermediate_video
        ]
        
        print("Step 1: Concatenating videos...")
        try:
            subprocess.run(concat_cmd, check=True, capture_output=True)
            print("Video concatenation successful")
        except subprocess.CalledProcessError as e:
            print(f"Concatenation failed: {e.stderr}")
            return {"error": "Video concatenation failed"}

        # Step 2: Process the concatenated video with effects
        output_video_path = os.path.join(temp_dir, "final_video.mp4")
        
        # Build filter chain for the single concatenated video
        video_filters = []
        
        # Scale, speed up, and standardize the concatenated video
        video_filters.append("scale=1080:1920:force_original_aspect_ratio=decrease")
        video_filters.append("pad=1080:1920:(ow-iw)/2:(oh-ih)/2")
        video_filters.append("setsar=1")
        video_filters.append("fps=30")
        video_filters.append("setpts=0.5*PTS")  # 2x speed
        
        # Add captions if provided
        if words_data:
            for word_info in words_data:
                clean_text = ffmpeg_escape(word_info['text'])
                start, end = word_info['start'], word_info['end']
                font_path = '/usr/share/fonts/truetype/Anton-Regular.ttf'
                video_filters.append(
                    f"drawtext=fontfile='{font_path}':text='{clean_text}':fontcolor=white:fontsize=120:borderw=8:bordercolor=black:x=(w-text_w)/2:y=(h-text_h)/2+h*0.2:enable='between(t,{start},{end})'"
                )
        
        video_filter_string = ",".join(video_filters)
        
        # Build final FFmpeg command
        final_cmd = [
            'ffmpeg', '-y', '-threads', '0',
            '-stream_loop', '-1', '-i', intermediate_video,  # Loop video
            '-i', narration_audio_path
        ]
        
        # Add background music if provided
        if background_music_path:
            final_cmd.extend(['-stream_loop', '-1', '-i', background_music_path])
            audio_filter = f"[1:a]volume=1.0[narration];[2:a]volume=0.25[bgm];[narration][bgm]amix=inputs=2:duration=first[final_a]"
            final_cmd.extend(['-filter_complex', f"[0:v]{video_filter_string}[final_v];{audio_filter}"])
            final_cmd.extend(['-map', '[final_v]', '-map', '[final_a]'])
        else:
            final_cmd.extend(['-filter_complex', f"[0:v]{video_filter_string}[final_v]"])
            final_cmd.extend(['-map', '[final_v]', '-map', '1:a'])
        
        # Output settings optimized for speed
        final_cmd.extend([
            '-c:v', 'libx264',
            '-preset', 'faster',  # Faster encoding
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '192k',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-t', str(narration_duration),  # Limit to narration duration
            output_video_path
        ])
        
        print("Step 2: Processing final video with effects...")
        try:
            result = subprocess.run(final_cmd, check=True, capture_output=True, text=True)
            print("Final video processing successful")
        except subprocess.CalledProcessError as e:
            print("Final processing failed.")
            print("FFmpeg STDERR:", e.stderr)
            return {"error": "Final video processing failed.", "details": e.stderr}

        print(f"Uploading final video from {output_video_path}...")
        final_url = upload_to_r2(output_video_path) 
        if not final_url:
            return {"error": "Video was generated but failed to upload."}
        
        return { "video_url": final_url, "filename": os.path.basename(output_video_path) }

# Start the RunPod serverless handler
runpod.serverless.start({"handler": handler})