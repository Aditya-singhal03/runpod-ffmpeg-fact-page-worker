import runpod
import subprocess
import os
import base64
import tempfile
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
            ExtraArgs={'ContentType': 'video/mp4'} # Set the correct content type
        )
        
        # 5. Construct the final public URL.
        # Ensure the base URL doesn't have a trailing slash.
        final_url = f"{public_url_base.rstrip('/')}/{quote(object_name)}"
        
        print(f"File uploaded successfully. Publlic URL: {final_url}")
        return final_url

    except Exception as e:
        print(f"An unexpected error occurred during R2 upload: {e}")
        return None

def ffmpeg_escape(text):
    """Cleans text for use in an FFmpeg drawtext filter's text option."""
    text = str(text)
    # Remove characters that can break the filter syntax.
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


async def handler(job):
    job_input = job['input']
    print(f"Job Input Received: {job['id']}")

    with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
        # --- 1. Download and Prepare Inputs (No change here) ---
        input_video_paths = []
        for i, video_url in enumerate(job_input.get("video_urls", [])):
            video_path = os.path.join(temp_dir, f"video_{i}.mp4")
            if not download_file(video_url, video_path):
                return {"error": f"Failed to download video from {video_url}"}
            input_video_paths.append(video_path)

        narration_audio_path = os.path.join(temp_dir, "narration.wav")
        audio_base64 = job_input.get("narration_audio_base64")
        if not audio_base64: return {"error": "Missing narration_audio_base64"}
        with open(narration_audio_path, "wb") as f:
            f.write(base64.b64decode(audio_base64))
        
        caption_data = job_input.get("caption_data", {})
        words_data = caption_data.get("words", [])

        background_music_path = None
        if job_input.get("background_music_url"):
            background_music_path = os.path.join(temp_dir, "background_music.mp3")
            if not download_file(job_input["background_music_url"], background_music_path):
                return {"error": f"Failed to download background music from {job_input['background_music_url']}"}

        # --- 2. Build the FFmpeg Command and Filter Graph ---
        ffmpeg_cmd = ['ffmpeg', '-y', '-threads', '0']  # Use all available CPU cores
        
        # Add all video inputs first
        for path in input_video_paths:
            ffmpeg_cmd.extend(['-i', path])
        
        # Add narration audio input
        narration_input_index = len(input_video_paths)
        ffmpeg_cmd.extend(['-i', narration_audio_path])
        
        # Add background music input with looping enabled
        music_input_index = -1
        if background_music_path:
            ffmpeg_cmd.extend(['-stream_loop', '-1', '-i', background_music_path])
            music_input_index = narration_input_index + 1

        filter_chains = []
        
        # --- FIXED LOGIC TO ELIMINATE VIDEO FREEZES ---

        # Chain 1: Standardize EACH input video with consistent encoding properties
        # Key additions: consistent pixel format and proper frame handling
        standardized_streams = []
        for i in range(len(input_video_paths)):
            stream_label = f"[v{i}]"
            standardized_streams.append(stream_label)
            # Enhanced standardization - removed heavy minterpolate, kept essential fixes
            filter_chains.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p{stream_label}")

        # Chain 2: Stitch the standardized video streams with proper transition handling
        video_streams_to_concat = "".join(standardized_streams)
        filter_chains.append(f"{video_streams_to_concat}concat=n={len(input_video_paths)}:v=1:a=0:unsafe=1[stitched_v]")

        # Chain 3: Speed up the stitched video stream - fast and efficient
        filter_chains.append("[stitched_v]setpts=0.5*PTS[fast_v]")

        # Chain 4: Create a silent, black video that is the same length as the NARRATION.
        filter_chains.append(f"color=c=black@0.0:s=1080x1920:d=9999[caption_canvas]")

        # Chain 5: Draw the captions onto the black canvas.
        current_caption_stream = "[caption_canvas]"
        if words_data:
            for i, word_info in enumerate(words_data):
                clean_text = ffmpeg_escape(word_info['text'])
                start, end = word_info['start'], word_info['end']
                font_path = '/usr/share/fonts/truetype/Anton-Regular.ttf'
                escaped_font_path = font_path.replace('\\', '/').replace(':', '\\:')
                output_stream_label = f"[c_caption_{i}]"
                filter_chain_link = f"{current_caption_stream}drawtext=fontfile='{escaped_font_path}':text='{clean_text}':fontcolor=white:fontsize=120:borderw=8:bordercolor=black:x=(w-text_w)/2:y=(h-text_h)/2+h*0.2:enable='between(t,{start},{end})'{output_stream_label}"
                filter_chains.append(filter_chain_link)
                current_caption_stream = output_stream_label
        
        # Chain 6: Overlay the captioned canvas on top of the sped-up video.
        filter_chains.append(f"[fast_v]{current_caption_stream}overlay=0:0[final_v]")
        final_video_map = "[final_v]"

        # Audio Mixing Chain (unchanged)
        if background_music_path:
            filter_chains.append(f"[{narration_input_index}:a]volume=1.0[narration];[{music_input_index}:a]volume=0.25[bgm];[narration][bgm]amix=inputs=2:duration=first[final_a]")
            audio_map = "[final_a]"
        else:
            audio_map = f"[{narration_input_index}:a]"

        # --- END OF FIXED LOGIC ---
        
        # --- 3. Build and Execute the FFmpeg Command ---
        filter_script_path = os.path.join(temp_dir, "filters.txt")
        with open(filter_script_path, "w") as f:
            f.write(";\n".join(filter_chains))
        
        ffmpeg_cmd.extend(['-filter_complex_script', filter_script_path])
        ffmpeg_cmd.extend(['-map', final_video_map])
        ffmpeg_cmd.extend(['-map', audio_map])
        
        output_video_path = os.path.join(temp_dir, "final_video.mp4")
        # Enhanced encoding settings for smooth playback with better threading
        ffmpeg_cmd.extend([
            '-c:v', 'libx264', 
            '-preset', 'faster',      # Changed from 'medium' to 'faster' for speed
            '-threads', '0',          # Use all CPU cores for encoding
            '-crf', '23',         
            '-g', '60',           
            '-keyint_min', '30',  
            '-c:a', 'aac', 
            '-b:a', '192k', 
            '-pix_fmt', 'yuv420p', 
            '-movflags', '+faststart',  
            '-shortest', 
            output_video_path
        ])
        
        # Execute and Upload steps (unchanged)
        print(f"Executing FFmpeg with filter script: {filter_script_path}")
        try:
            result = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
            print("FFmpeg execution successful.")
        except subprocess.CalledProcessError as e:
            print("FFmpeg execution failed.")
            print("FFmpeg STDERR:", e.stderr)
            return {"error": "FFmpeg processing failed.", "details": e.stderr}

        print(f"Uploading final video from {output_video_path}...")
        final_url = upload_to_r2(output_video_path) 
        if not final_url:
            return {"error": "Video was generated but failed to upload."}
        
        return { "video_url": final_url, "filename": os.path.basename(output_video_path) }

# Start the RunPod serverless handler
runpod.serverless.start({"handler": handler})