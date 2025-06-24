import runpod
import subprocess
import os
import base64
import tempfile
import json
import requests
from urllib.parse import quote

def upload_to_gofile(file_path):
    """Uploads a file to a specific GoFile.io account and returns the direct download link."""
    
    # 1. Get secrets from environment variables. This is the secure way.
    api_token = os.environ.get('GOFILE_API_TOKEN')
    folder_id = os.environ.get('GOFILE_FOLDER_ID') # Optional: The ID of a folder in your account

    if not api_token:
        print("ERROR: GOFILE_API_TOKEN environment variable not set.")
        return None

    try:
        # 2. Get the best available server. No change here.
        server_response = requests.get("https://api.gofile.io/servers")
        server_response.raise_for_status()
        server = server_response.json()["data"]["servers"][0]["name"]
        print(f"GoFile server selected: {server}")

        # 3. Prepare the authenticated upload request.
        with open(file_path, 'rb') as f:
            files = {'file': (os.path.basename(file_path), f)}
            
            headers = {'Authorization': f'Bearer {api_token}'}
            data = {'folderId': folder_id} if folder_id else {}

            # The upload request now sends a `data` payload alongside the `files`.
            upload_response = requests.post(
                f"https://{server}.gofile.io/uploadFile", 
                files=files,
                headers=headers,
                data=data
            )
        
        print(os.path.basename(file_path))
        upload_response.raise_for_status()
        upload_data = upload_response.json()["data"]
        dashboard_link = upload_data["downloadPage"]
        content_id = upload_data["id"]
        filename = os.path.basename(file_path)
        encoded_filename = quote(filename)
        final_link = f"https://{server}.gofile.io/download/web/{content_id}/{encoded_filename}"
        
        return dashboard_link , final_link

    except requests.exceptions.RequestException as e:
        print(f"Error uploading to GoFile.io: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during upload: {e}")
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
        if not audio_base64: return {"error": "Missing narration_audio_base64"}
        with open(narration_audio_path, "wb") as f:
            f.write(base64.b64decode(audio_base64))
        
        caption_data = job_input.get("caption_data", {})
        words_data = caption_data.get("words", [])

        # --- 2. Write the Complex Filter Graph to a File ---
        filter_script_path = os.path.join(temp_dir, "filters.txt")
        with open(filter_script_path, "w") as f:
            filter_chains = []

            # --- THE FIX IS HERE ---
            # Chain 1: Standardize EACH input video BEFORE concatenation.
            # We scale each video to 1080x1920 and ensure it has a constant 30fps framerate.
            standardized_streams = []
            for i in range(len(input_video_paths)):
                stream_label = f"[v{i}]"
                standardized_streams.append(stream_label)
                filter_chains.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30{stream_label}")

            # Chain 2: Stitch the now-standardized video streams together
            video_streams = "".join(standardized_streams)
            # The concat filter now also discards the original audio from the source videos (a=0)
            filter_chains.append(f"{video_streams}concat=n={len(input_video_paths)}:v=1:a=0[stitched_v]")

            # Chain 3: Speed up the final stitched video
            # We no longer need to scale/format here as it was done in step 1.
            filter_chains.append("[stitched_v]setpts=0.5*PTS[formatted_v]")

            # Chain 4: Add animated captions
            current_video_stream = "[formatted_v]"
            if words_data:
                for i, word_info in enumerate(words_data):
                    clean_text = ffmpeg_escape(word_info['text'])
                    start, end = word_info['start'], word_info['end']
                    font_path = '/usr/share/fonts/truetype/Anton-Regular.ttf'
                    escaped_font_path = font_path.replace('\\', '/').replace(':', '\\:')
                    output_stream_label = f"[v_caption_{i}]"
                    
                    filter_chain_link = (
                        f"{current_video_stream}"
                        f"drawtext="
                        f"fontfile='{escaped_font_path}':"
                        f"text='{clean_text}':"
                        f"fontcolor=white:fontsize=120:borderw=8:bordercolor=black:"
                        f"x=(w-text_w)/2:"
                        f"y=(h-text_h)/2 + h*0.2:"
                        f"enable='between(t,{start},{end})'"
                        f"{output_stream_label}"
                    )
                    filter_chains.append(filter_chain_link)
                    current_video_stream = output_stream_label
            
            final_video_map = current_video_stream
            
            # Write the final, correctly joined filter graph to the script file
            f.write(";\n".join(filter_chains))

        # --- 3. Build and Execute the FFmpeg Command ---
        ffmpeg_cmd = ['ffmpeg', '-y']
        for path in input_video_paths:
            ffmpeg_cmd.extend(['-i', path])
        ffmpeg_cmd.extend(['-i', narration_audio_path])
        
        ffmpeg_cmd.extend(['-filter_complex_script', filter_script_path])
        
        audio_input_index = len(input_video_paths)
        ffmpeg_cmd.extend(['-map', final_video_map])
        ffmpeg_cmd.extend(['-map', f'{audio_input_index}:a'])
        
        output_video_path = os.path.join(temp_dir, "final_video.mp4")
        ffmpeg_cmd.extend([
            '-c:v', 'libx264', '-profile:v', 'main', '-preset', 'medium', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k', '-pix_fmt', 'yuv420p', '-shortest',
            output_video_path
        ])

        print(f"Executing FFmpeg with filter script: {filter_script_path}")
        try:
            result = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
            print("FFmpeg execution successful.")
        except subprocess.CalledProcessError as e:
            print("FFmpeg execution failed.")
            print("FFmpeg STDERR:", e.stderr)
            return {"error": "FFmpeg processing failed.", "details": e.stderr}

        print(f"Uploading final video from {output_video_path}...")
        dashboard_link, final_link = upload_to_gofile(output_video_path)

        if not dashboard_link:
            return {"error": "Video was generated but failed to upload."}
        
        # --- 5. Return the URL, not the file data ---
        return {
            "video_url": final_link,
            "dashboard_url": dashboard_link,
            "filename": os.path.basename(output_video_path)
        }

# Start the RunPod serverless handler
runpod.serverless.start({"handler": handler})