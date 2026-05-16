import os
import subprocess
import uuid
from flask import Flask, request, send_file, render_template_string

app = Flask(__name__)
# 100 MB tak ki limit allow karne ke liye (Aapki 30-45MB files aaram se aayengi)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 

# Ek hi file mein HTML aur UI Design
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cloud Video Render</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f0fdf4; color: #064e3b; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .glass-panel { background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(10px); padding: 30px; border-radius: 20px; box-shadow: 0 8px 32px rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); width: 90%; max-width: 400px; text-align: center; }
        h2 { margin-top: 0; color: #047857; }
        label { display: block; text-align: left; margin: 15px 0 5px; font-weight: bold; font-size: 14px; }
        input[type="file"] { width: 100%; padding: 10px; border: 1px dashed #34d399; border-radius: 10px; background: #ecfdf5; box-sizing: border-box; cursor: pointer; }
        button { margin-top: 25px; width: 100%; background-color: #10b981; color: white; border: none; padding: 12px; font-size: 16px; font-weight: bold; border-radius: 10px; cursor: pointer; transition: 0.3s; }
        button:hover { background-color: #059669; }
        #loader { display: none; margin-top: 15px; font-weight: bold; color: #059669; }
    </style>
</head>
<body>
    <div class="glass-panel">
        <h2>🎥 Auto Video Maker</h2>
        <form action="/process" method="post" enctype="multipart/form-data" onsubmit="document.getElementById('loader').style.display='block'; document.getElementById('btn').style.display='none';">
            <label>1. Select Cover Photo:</label>
            <input type="file" name="image" accept="image/*" required>
            
            <label>2. Select Audio (30-45 MB):</label>
            <input type="file" name="audio" accept="audio/*" required>
            
            <button type="submit" id="btn">⚡ Render Video</button>
            <div id="loader">⏳ Server is Rendering... Please wait...</div>
        </form>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/process', methods=['POST'])
def process():
    if 'image' not in request.files or 'audio' not in request.files:
        return "Files upload nahi hui!", 400
        
    img_file = request.files['image']
    aud_file = request.files['audio']
    
    # Har render ke liye ek unique naam banayenge
    task_id = str(uuid.uuid4())[:6]
    img_path = f"{task_id}_img.jpg"
    aud_path = f"{task_id}_aud.mp3"
    out_path = f"{task_id}_video.mp4"
    
    # Files ko server par save karna
    img_file.save(img_path)
    aud_file.save(aud_path)
    
    try:
        # Render ke hardware par ultra-fast FFmpeg command chalana
        command = [
            'ffmpeg', '-loop', '1', '-framerate', '1',
            '-i', img_path, '-i', aud_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage',
            '-c:a', 'copy', '-shortest', out_path, '-y'
        ]
        subprocess.run(command, check=True)
        
        # Original inputs ko delete kar dena taaki space bache
        os.remove(img_path)
        os.remove(aud_path)
        
        # Video file ko direct browser mein download ke liye bhej dena
        return send_file(out_path, as_attachment=True, download_name="Novel_Video_Ready.mp4")
        
    except Exception as e:
        return f"Error aaya: {str(e)}", 500

if __name__ == '__main__':
    # Render port automatically assign karta hai
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
