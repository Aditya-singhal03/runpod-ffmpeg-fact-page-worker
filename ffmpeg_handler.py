import runpod
import subprocess
import os
import base64
import tempfile
import json
import requests
import asyncio

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
        if not audio_base64:
            return {"error": "Missing narration_audio_base64"}
        with open(narration_audio_path, "wb") as f:
            f.write(base64.b64decode(audio_base64))
        
        caption_data = job_input.get("caption_data", {})
        words_data = caption_data.get("words", [])

        # --- 2. Write the Complex Filter Graph to a File ---
        filter_script_path = os.path.join(temp_dir, "filters.txt")
        with open(filter_script_path, "w") as f:
            # Chain 1: Stitch video streams
            video_streams = "".join([f"[{i}:v]" for i in range(len(input_video_paths))])
            f.write(f"{video_streams}concat=n={len(input_video_paths)}:v=1:a=0[stitched_v];\n")

            # Chain 2: Speed up, normalize framerate, and format for Reels
            f.write("[stitched_v]setpts=0.5*PTS,fps=30,scale=1080:1920,format=yuv420p[formatted_v];\n")

            # Chain 3: Add animated captions
            current_video_stream = "[formatted_v]"
            if words_data:
                # --- THIS IS THE CLEANED-UP LOOP ---
                for i, word_info in enumerate(words_data):
                    clean_text = ffmpeg_escape(word_info['text'])
                    start = word_info['start']
                    end = word_info['end']
                    
                    # Define the font path inside the Docker container
                    # This path is set in your Dockerfile.
                    font_path = '/usr/share/fonts/truetype/Anton-Regular.ttf'
                    
                    # Escape the path for safety (good practice)
                    escaped_font_path = font_path.replace('\\', '/').replace(':', '\\:')
                    
                    output_stream_label = f"[v_caption_{i}]"
                    
                    filter_line = (
                        f"{current_video_stream}"
                        f"drawtext="
                        f"fontfile='{escaped_font_path}':"
                        f"text='{clean_text}':"
                        f"fontcolor=white:fontsize=120:borderw=8:bordercolor=black:"
                        f"x=(w-text_w)/2:"
                        f"y=(h-text_h)/2 + h*0.2:"
                        f"enable='between(t,{start},{end})'"
                        f"{output_stream_label};\n"
                    )
                    f.write(filter_line)
                    current_video_stream = output_stream_label
        
        final_video_map = current_video_stream

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

        with open(output_video_path, "rb") as f:
            video_data = f.read()
        base64_video = base64.b64encode(video_data).decode('utf-8')
        return { "video_base64": base64_video, "filename": os.path.basename(output_video_path) }

# Start the RunPod serverless handler
runpod.serverless.start({"handler": handler})
# =====================================================================================
#  LOCAL TESTING BLOCK 
# =====================================================================================
if __name__ == '__main__':
    # ... (Your local testing block remains the same) ...
    try:
        with open('audio_for_test.b64', 'r') as f:
            test_audio_base64 = f.read()
    except FileNotFoundError:
        print("Error: 'audio_for_test.b64' file not found.")
        exit(1)

    mock_job = {
        "id": "local-test-job",
        "input": {
            "video_urls": [
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v1.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v2.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v3.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v4.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v5.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v6.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v7.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v8.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v9.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v10.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v11.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v12.mp4",
                "https://raw.githubusercontent.com/Aditya-singhal03/rawFiles/main/v13.mp4",
            ],
            "narration_audio_base64": test_audio_base64,
            "caption_data": {
                "words": [{"end":0.74,"start":0.3,"text":"Imagine"},{"end":0.98,"start":0.74,"text":"if"},{"end":1.26,"start":0.98,"text":"you"},{"end":1.58,"start":1.26,"text":"had"},{"end":2.08,"start":1.58,"text":"three"},{"end":2.52,"start":2.08,"text":"hearts,"},{"end":3.2,"start":2.8,"text":"and"},{"end":3.38,"start":3.2,"text":"you"},{"end":3.68,"start":3.38,"text":"could"},{"end":4.26,"start":3.68,"text":"simply"},{"end":4.6,"start":4.26,"text":"switch"},{"end":4.94,"start":4.6,"text":"one"},{"end":5.32,"start":4.94,"text":"off"},{"end":5.72,"start":5.32,"text":"whenever"},{"end":6,"start":5.72,"text":"you"},{"end":6.28,"start":6,"text":"felt"},{"end":6.52,"start":6.28,"text":"like"},{"end":6.66,"start":6.52,"text":"you"},{"end":6.9,"start":6.66,"text":"needed"},{"end":7.06,"start":6.9,"text":"a"},{"end":7.38,"start":7.06,"text":"break."},{"end":8.2,"start":7.96,"text":"Sounds"},{"end":8.5,"start":8.2,"text":"like"},{"end":8.86,"start":8.5,"text":"science"},{"end":9.14,"start":8.86,"text":"fiction,"},{"end":9.52,"start":9.32,"text":"right?"},{"end":10.32,"start":10.06,"text":"But"},{"end":10.58,"start":10.32,"text":"deep"},{"end":10.8,"start":10.58,"text":"in"},{"end":10.94,"start":10.8,"text":"the"},{"end":11.28,"start":10.94,"text":"ocean,"},{"end":11.76,"start":11.6,"text":"this"},{"end":12.1,"start":11.76,"text":"is"},{"end":12.8,"start":12.1,"text":"exactly"},{"end":13.12,"start":12.8,"text":"what"},{"end":13.46,"start":13.12,"text":"the"},{"end":14.02,"start":13.46,"text":"octopus"},{"end":14.48,"start":14.02,"text":"does."},{"end":15.2,"start":15.02,"text":"It"},{"end":15.48,"start":15.2,"text":"has"},{"end":15.76,"start":15.48,"text":"not"},{"end":16.08,"start":15.76,"text":"one,"},{"end":16.7,"start":16.48,"text":"not"},{"end":17.14,"start":16.7,"text":"two,"},{"end":17.74,"start":17.46,"text":"but"},{"end":18.28,"start":17.74,"text":"three"},{"end":18.74,"start":18.28,"text":"hearts."},{"end":19.58,"start":19.28,"text":"And"},{"end":19.76,"start":19.58,"text":"when"},{"end":19.98,"start":19.76,"text":"it"},{"end":20.36,"start":19.98,"text":"swims,"},{"end":20.88,"start":20.74,"text":"it"},{"end":21.46,"start":20.88,"text":"literally"},{"end":21.84,"start":21.46,"text":"shuts"},{"end":22.24,"start":21.84,"text":"one"},{"end":22.52,"start":22.24,"text":"heart"},{"end":23.08,"start":22.52,"text":"down"},{"end":23.44,"start":23.08,"text":"to"},{"end":23.8,"start":23.44,"text":"save"},{"end":24.28,"start":23.8,"text":"energy."},{"end":25.02,"start":24.84,"text":"The"},{"end":25.24,"start":25.02,"text":"other"},{"end":25.58,"start":25.24,"text":"two,"},{"end":26.22,"start":25.82,"text":"they"},{"end":26.6,"start":26.22,"text":"keep"},{"end":26.78,"start":26.6,"text":"it"},{"end":27.24,"start":26.78,"text":"alive."},{"end":28.2,"start":27.84,"text":"Then,"},{"end":28.6,"start":28.44,"text":"when"},{"end":28.84,"start":28.6,"text":"it"},{"end":29.18,"start":28.84,"text":"stops"},{"end":29.6,"start":29.18,"text":"moving"},{"end":29.96,"start":29.6,"text":"and"},{"end":30.44,"start":29.96,"text":"rests,"},{"end":31.12,"start":30.82,"text":"that"},{"end":31.5,"start":31.12,"text":"third"},{"end":31.86,"start":31.5,"text":"heart"},{"end":32.3,"start":31.86,"text":"kicks"},{"end":32.72,"start":32.3,"text":"back"},{"end":33.08,"start":32.72,"text":"in,"},{"end":33.64,"start":33.34,"text":"helping"},{"end":33.96,"start":33.64,"text":"the"},{"end":34.38,"start":33.96,"text":"octopus"},{"end":35,"start":34.38,"text":"recover"},{"end":35.42,"start":35,"text":"and"},{"end":36.03,"start":35.42,"text":"recharge."},{"end":37,"start":36.03,"text":"A"},{"end":37.56,"start":37,"text":"natural"},{"end":38.04,"start":37.56,"text":"stress"},{"end":38.46,"start":38.04,"text":"management"},{"end":39,"start":38.46,"text":"system"},{"end":39.6,"start":39,"text":"built"},{"end":39.94,"start":39.6,"text":"into"},{"end":40.14,"start":39.94,"text":"its"},{"end":40.58,"start":40.14,"text":"body"},{"end":40.94,"start":40.58,"text":"that"},{"end":41.12,"start":40.94,"text":"lets"},{"end":41.34,"start":41.12,"text":"it"},{"end":41.54,"start":41.34,"text":"stay"},{"end":42.04,"start":41.54,"text":"calm,"},{"end":42.72,"start":42.38,"text":"conserve"},{"end":43.3,"start":42.72,"text":"energy,"},{"end":43.9,"start":43.66,"text":"and"},{"end":44.48,"start":43.9,"text":"survive"},{"end":44.98,"start":44.48,"text":"even"},{"end":45.28,"start":44.98,"text":"in"},{"end":45.54,"start":45.28,"text":"the"},{"end":46.16,"start":45.54,"text":"harshest"},{"end":46.8,"start":46.16,"text":"environments."},{"end":48.12,"start":47.92,"text":"And"},{"end":48.46,"start":48.12,"text":"what's"},{"end":48.84,"start":48.46,"text":"truly"},{"end":49.4,"start":48.84,"text":"amazing"},{"end":50.06,"start":49.4,"text":"is"},{"end":50.34,"start":50.06,"text":"how"},{"end":50.62,"start":50.34,"text":"we"},{"end":50.84,"start":50.62,"text":"could"},{"end":51.14,"start":50.84,"text":"learn"},{"end":51.36,"start":51.14,"text":"from"},{"end":51.8,"start":51.36,"text":"this."},{"end":52.7,"start":52.42,"text":"To"},{"end":53.12,"start":52.7,"text":"switch"},{"end":53.8,"start":53.12,"text":"off,"},{"end":54.44,"start":54.22,"text":"to"},{"end":55.18,"start":54.44,"text":"recharge,"},{"end":55.98,"start":55.78,"text":"to"},{"end":56.28,"start":55.98,"text":"not"},{"end":56.54,"start":56.28,"text":"run"},{"end":57.16,"start":56.54,"text":"ourselves"},{"end":57.54,"start":57.16,"text":"to"},{"end":58.08,"start":57.54,"text":"exhaustion,"},{"end":58.62,"start":58.48,"text":"and"},{"end":58.84,"start":58.62,"text":"to"},{"end":59.18,"start":58.84,"text":"focus"},{"end":59.42,"start":59.18,"text":"our"},{"end":59.78,"start":59.42,"text":"energy"},{"end":60.26,"start":59.78,"text":"only"},{"end":60.5,"start":60.26,"text":"when"},{"end":60.78,"start":60.5,"text":"it"},{"end":61.16,"start":60.78,"text":"truly"},{"end":61.8,"start":61.16,"text":"matters."},{"end":62.76,"start":62.18,"text":"Because"},{"end":63.14,"start":62.76,"text":"if"},{"end":63.4,"start":63.14,"text":"we"},{"end":63.74,"start":63.4,"text":"mastered"},{"end":64.14,"start":63.74,"text":"that,"},{"end":64.62,"start":64.36,"text":"just"},{"end":64.82,"start":64.62,"text":"like"},{"end":65.08,"start":64.82,"text":"the"},{"end":65.58,"start":65.08,"text":"octopus,"},{"end":66.22,"start":65.92,"text":"we"},{"end":66.48,"start":66.22,"text":"could"},{"end":66.88,"start":66.48,"text":"become"},{"end":67.6,"start":66.88,"text":"stronger,"},{"end":68.44,"start":68.06,"text":"smarter,"},{"end":69.04,"start":68.84,"text":"and"},{"end":69.46,"start":69.04,"text":"more"},{"end":70.05,"start":69.46,"text":"resilient."},{"end":71.16,"start":70.05,"text":"So"},{"end":71.36,"start":71.16,"text":"if"},{"end":71.54,"start":71.36,"text":"you"},{"end":71.76,"start":71.54,"text":"want"},{"end":72.14,"start":71.76,"text":"more"},{"end":72.46,"start":72.14,"text":"wild"},{"end":72.76,"start":72.46,"text":"nature"},{"end":73.12,"start":72.76,"text":"facts"},{"end":73.5,"start":73.12,"text":"that'll"},{"end":73.92,"start":73.5,"text":"change"},{"end":74.1,"start":73.92,"text":"the"},{"end":74.28,"start":74.1,"text":"way"},{"end":74.52,"start":74.28,"text":"you"},{"end":74.78,"start":74.52,"text":"think,"},{"end":75.5,"start":75.18,"text":"follow"},{"end":75.78,"start":75.5,"text":"this"},{"end":76.22,"start":75.78,"text":"page,"},{"end":77,"start":76.64,"text":"because"},{"end":77.42,"start":77,"text":"every"},{"end":77.76,"start":77.42,"text":"reel"},{"end":78.06,"start":77.76,"text":"will"},{"end":78.36,"start":78.06,"text":"open"},{"end":78.64,"start":78.36,"text":"up"},{"end":78.86,"start":78.64,"text":"a"},{"end":79.1,"start":78.86,"text":"whole"},{"end":79.34,"start":79.1,"text":"new"},{"end":79.66,"start":79.34,"text":"world"},{"end":79.9,"start":79.66,"text":"for"},{"end":80.12,"start":79.9,"text":"you."}]
            }
        }
    }

    print("--- Starting Local Test ---")
    result = asyncio.run(handler(mock_job))

    if "error" in result:
        print(f"\n--- Test Failed ---")
        print(f"Error: {result['error']}")
        if "details" in result:
            print(f"Details: {result['details']}")
    else:
        output_filename = "local_test_output.mp4"
        with open(output_filename, "wb") as f:
            f.write(base64.b64decode(result['video_base64']))
        
        print(f"\n--- Test Succeeded! ---")
        print(f"Final video saved as '{output_filename}'. Please open and review it.")