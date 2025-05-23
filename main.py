print("[INFO]: Loading Libs...")
from flask import Flask, Response, request, jsonify, render_template
import cv2
import numpy as np
import json
from dotenv import dotenv_values
import time
import threading
import queue
import ffmpeg
import easyocr
import av
import os
import sys
import datetime
print("[INFO]: Libs loaded.")

# suppresses print
class suppress_stdout_stderr:
    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stdout = self._stdout

print ("[EASYOCR]: Loading Loading EN language model...")
with suppress_stdout_stderr():
    global reader
    reader = easyocr.Reader(['en'])
print ("[EASYOCR]: EN language model loaded.")

print("[INFO]: Loading Environment Variables and Configs...")
_env = dotenv_values(".env") 

options_file = '/data/options.json'
if os.path.exists(options_file):
    with open(options_file, 'r') as f:
        config = json.load(f)
    print("HASS configuration loaded successfully.")
else:
    print(f"{options_file} not found. Ensure the add-on has been started.")

app = Flask(__name__)

OCR_RESULTS_FILE = "/data/ocr_results.json"
RTSP_URL = config.get('rtsp_url', "rtsp://user:password@ip:port/location" )
DEBUG_MODE = config.get('debug_mode', False)
LITERS_PER_HOUR = config.get('liters_per_hour', 10.0)  # Default liters per hour if not set

print(f"[INFO]: RTSP URL: {RTSP_URL}, Debug Mode: {DEBUG_MODE}")



DISABLE_RTSP = False

if ("DISABLE_RTSP" in _env):
    DISABLE_RTSP=bool(_env["DISABLE_RTSP"])
    if (DISABLE_RTSP):
        print("[INFO]: RTSP is disabled.")

SETTINGS_FILE = "/data/settings.json"
BOXES_FILE = "/data/boxes.json"

# Standardwerte für die Einstellungen
default_settings = {
    "rotation": 0,
    "contrast": 1.0,
    "brightness": 0,
    "crop_top": 0,
    "crop_bottom": 0,
    "crop_left": 0,
    "crop_right": 0,
    "box_top": 100,
    "box_left": 100,
    "box_width": 150,
    "box_height": 150
}
print("[INFO]: Environment Variables and Configs loaded.")


def ocrThreadFunc(result_queue):
    print("[OCR]: Starting an OCR task.")
    reader = easyocr.Reader(['en'])
    result = reader.readtext('./computed.png', allowlist="0123456789")
    value = {
        "number": result[0][1],
        "certainty": result[0][2]
    }
    result_queue.put(value)
    print("[OCR]: End Task")

print("[INFO]: Starting the server...")

# Laden der Einstellungen aus einer Datei oder Nutzung der Standardwerte
def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_settings

# Speichern der Einstellungen in eine Datei
def save_settings(settings):
    with open(SETTINGS_FILE, "w") as file:
        json.dump(settings, file, indent=4)

# Load the OCR results from a file or initialize with default values
def load_ocr_results():
    try:
        with open(OCR_RESULTS_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"number": None, "certainty": 0, "timestamp": None}

# Save the OCR results to a file
def save_ocr_results(results):
    with open(OCR_RESULTS_FILE, "w") as file:
        json.dump(results, file, indent=4)

# Laden der Boxen aus einer Datei oder Nutzung eines leeren Arrays
def load_boxes():
    try:
        with open(BOXES_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

# Speichern der Boxen in eine Datei
def save_boxes(boxes):
    with open(BOXES_FILE, "w") as file:
        json.dump(boxes, file, indent=4)

# Lade die Einstellungen und Boxen beim Start des Servers
settings = load_settings()
boxes = load_boxes()

def get_frame(displayBoxes=True):
    if DISABLE_RTSP:
        # Load image from file
        frame = cv2.imread("image.png")
    else:
        print(f"Trying to open the RTSP stream at {RTSP_URL}")
        try:
            # Use av.open with RTSP options
            container = av.open(RTSP_URL, options={"rtsp_transport": "tcp"})
            frame = None
            for packet in container.demux(video=0):
                for frame in packet.decode():
                    img = frame.to_image()  # Convert frame to PIL Image
                    frame = np.array(img)   # Convert to NumPy array
                    break  # Just grab one frame
                if frame is not None:
                    break
        except av.AVError as e:
            print(f"Error opening RTSP stream: {e}")
            return None
        
    # Apply rotation
    if settings["rotation"] != 0 and frame is not None:
        (h, w) = frame.shape[:2]
        center = (w // 2, h // 2)
        matrix = cv2.getRotationMatrix2D(center, settings["rotation"], 1.0)
        frame = cv2.warpAffine(frame, matrix, (w, h))

    # Apply contrast & brightness
    if frame is not None:
        frame = cv2.convertScaleAbs(frame, alpha=settings["contrast"], beta=settings["brightness"])

    # Apply cropping
    if frame is not None:
        h, w, _ = frame.shape
        top = min(settings["crop_top"], h)
        bottom = max(h - settings["crop_bottom"], 0)
        left = min(settings["crop_left"], w)
        right = max(w - settings["crop_right"], 0)
        frame = frame[top:bottom, left:right]

    # Draw boxes on the image
    if frame is not None:
        for box in boxes:
            box_top = box["box_top"]
            box_left = box["box_left"]
            box_width = box["box_width"]
            box_height = box["box_height"]
            color = (0, 255, 0)  # Default color
            if displayBoxes:
                cv2.rectangle(frame, (box_left, box_top), (box_left + box_width, box_top + box_height), color, 2)
                cv2.putText(frame, f'ID: {box["id"]}', (box_left, box_top - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        _, buffer = cv2.imencode(".jpg", frame)
        return buffer.tobytes()
    return None

@app.route('/readocr')
def readocr():
    result_queue = queue.Queue()

    def ocr_from_computed(result_queue):
        # Get the image from the /computed route
        response = app.test_client().get('/computed')
        if response.status_code != 200:
            result_queue.put({"error": "Failed to retrieve computed image"})
            return

        # Decode the image from the response
        np_frame = np.frombuffer(response.data, np.uint8)
        image = cv2.imdecode(np_frame, cv2.IMREAD_COLOR)
        if image is None:
            result_queue.put({"error": "Failed to decode computed image"})
            return

        result = reader.readtext(image, allowlist="0123456789")
        if not result:
            result_queue.put({"error": "No text detected"})
            return

        value = {
            "number": result[0][1],
            "certainty": result[0][2]
        }
        result_queue.put(value)

    # Start the OCR task in a separate thread
    ocr_thread = threading.Thread(target=ocr_from_computed, args=(result_queue,), daemon=True)
    ocr_thread.start()

    # Wait for the thread to finish
    ocr_thread.join()

    # Get the result from the queue
    result = result_queue.get()

    # Handle errors if any
    if "error" in result:
        return jsonify({"error": result["error"]}), 500

    # Load the previous OCR results
    ocr_results = load_ocr_results()

    # Ensure the new value does not decrease
    new_number = int(result["number"])
    previous_number = int(ocr_results["number"]) if ocr_results["number"] is not None else 0
    previous_ocr_certainty = ocr_results["certainty"] if ocr_results["certainty"] is not None else 0
    previous_ocr_timestamp = ocr_results["timestamp"] if ocr_results["timestamp"] is not None else None

    if new_number < previous_number:
        return jsonify({
            "error": "OCR value cannot decrease. Expected: >= " + str(previous_number) + " Received: " + str(new_number),
            "number": previous_number,
            "certainty": previous_ocr_certainty,
            "timestamp": ocr_results["timestamp"],
            "timezone": "UTC+0"

        }), 400

    # check if the results match up with the liters per hour
    maximum_expected_value = int((LITERS_PER_HOUR / 3600) * OCR_INTERVAL)
    if new_number > previous_number + maximum_expected_value:
        return jsonify({
            "error": "OCR value exceeds expected range. Expected: <= " + str(previous_number + maximum_expected_value) + " Received: " + str(new_number),
            "number": previous_number,
            "certainty": previous_ocr_certainty,
            "timestamp": ocr_results["timestamp"],
            "timezone": "UTC+0"
        }), 400
    # Update the OCR results with the new value, certainty, and timestamp
    ocr_results["number"] = new_number
    ocr_results["certainty"] = result["certainty"]
    ocr_results["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    # Save the updated OCR results
    save_ocr_results(ocr_results)

    # Return the result as JSON
    return jsonify({
        "number": ocr_results["number"],
        "certainty": ocr_results["certainty"],
        "timestamp": ocr_results["timestamp"],
        "timezone": "UTC+0"
    })

@app.route('/cachedocr')
def cachedocr():
    # Load the last OCR results
    ocr_results = load_ocr_results()
    if ocr_results["number"] is None:
        return jsonify({"error": "No OCR results available"}), 404

    return jsonify({
        "number": ocr_results["number"],
        "certainty": ocr_results["certainty"],
        "timestamp": ocr_results["timestamp"],
        "timezone": "UTC+0"
    })

@app.route('/snapshot')
def snapshot():
    frame = get_frame()
    if frame is None:
        return "Error: Could not retrieve frame", 500
    return Response(frame, mimetype='image/jpeg')

@app.route('/computed')
def computed():
    frame = get_frame(displayBoxes=False)
    if frame is None:
        return "Error: Could not retrieve frame", 500

    # Decode the frame into an image
    np_frame = np.frombuffer(frame, np.uint8)
    image = cv2.imdecode(np_frame, cv2.IMREAD_COLOR)

    if image is None:
        return "Error: Could not decode frame", 500

    # Extract and stitch the box regions
    snippets = []
    for box in boxes:
        box_top = box["box_top"]
        box_left = box["box_left"]
        box_width = box["box_width"]
        box_height = box["box_height"]

        # Ensure the box dimensions are within the image bounds
        snippet = image[box_top:box_top + box_height, box_left:box_left + box_width]
        if snippet.size > 0:  # Avoid empty regions
            snippets.append(snippet)

    if not snippets:
        return "Error: No valid boxes to process", 400

    # Resize all snippets to the same height
    target_height = max(snippet.shape[0] for snippet in snippets)  # Use the tallest snippet
    resized_snippets = [cv2.resize(snippet, (snippet.shape[1], target_height)) for snippet in snippets]

    # Concatenate snippets horizontally
    stitched_image = cv2.hconcat(resized_snippets)

    # Encode the stitched image to JPEG
    _, buffer = cv2.imencode(".jpg", stitched_image)
    return Response(buffer.tobytes(), mimetype='image/jpeg')

@app.route('/set_settings', methods=['POST'])
def set_settings():
    global settings
    data = request.json
    for key in settings.keys():
        if key in data:
            settings[key] = data[key]
    save_settings(settings)  # Speichert die Änderungen sofort
    return jsonify({"message": "Settings gespeichert", "settings": settings})

@app.route('/get_settings')
def get_settings():
    return jsonify(settings)

@app.route('/set_boxes', methods=['POST'])
def set_boxes():
    global boxes
    boxes = request.json
    save_boxes(boxes)
    return jsonify({"message": "Boxes gespeichert", "boxes": boxes})

@app.route('/get_boxes')
def get_boxes():
    return jsonify(boxes)

# Load OCR interval from configuration
OCR_INTERVAL = config.get('ocr_interval', 60)  # Default to 60 seconds if not set

# Variable to track the last OCR execution time
last_ocr_execution = time.time()

# Function to periodically trigger OCR reading
def periodic_ocr_task():
    global last_ocr_execution
    while True:
        print("[INFO]: Triggering periodic OCR reading...")
        try:
            response = app.test_client().get('/readocr')
            if response.status_code == 200:
                print("[INFO]: OCR reading successful:", response.json)
            else:
                print("[ERROR]: OCR reading failed:", response.json)
        except Exception as e:
            print("[ERROR]: Exception during periodic OCR reading:", e)
        last_ocr_execution = time.time()
        time.sleep(OCR_INTERVAL)

# Route to check the time remaining for the next OCR interval
@app.route('/get_next_ocr_interval')
def get_next_ocr_interval():
    global last_ocr_execution
    elapsed_time = time.time() - last_ocr_execution
    remaining_time = max(OCR_INTERVAL - elapsed_time, 0)
    return jsonify({
        "next_ocr_in_seconds": remaining_time,
        "last_execution_time": datetime.datetime.utcfromtimestamp(last_ocr_execution).strftime('%Y-%m-%d %H:%M:%S UTC')
    })

# Endpoint to update OCR interval dynamically
@app.route('/set_ocr_interval', methods=['POST'])
def set_ocr_interval():
    global OCR_INTERVAL
    data = request.json
    if 'ocr_interval' in data and isinstance(data['ocr_interval'], int) and data['ocr_interval'] > 0:
        OCR_INTERVAL = data['ocr_interval']
        print(f"[INFO]: OCR interval updated to {OCR_INTERVAL} seconds.")
        return jsonify({"message": "OCR interval updated", "ocr_interval": OCR_INTERVAL})
    return jsonify({"error": "Invalid OCR interval"}), 400

# Start the periodic OCR task in a separate thread
ocr_thread = threading.Thread(target=periodic_ocr_task, daemon=True)
ocr_thread.start()

@app.route('/calibrate', methods=['GET', 'POST'])
def calibrate():
    if request.method == 'POST':
        data = request.json
        if not data or 'number' not in data or 'timestamp' not in data:
            return jsonify({"error": "Invalid input. 'number' and 'timestamp' are required."}), 400

        try:
            # Validate and parse the timestamp
            timestamp = datetime.datetime.strptime(data['timestamp'], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({"error": "Invalid timestamp format. Use 'YYYY-MM-DD HH:MM:SS'."}), 400

        # Load the current OCR results
        ocr_results = load_ocr_results()

        # Update the OCR results
        ocr_results["number"] = int(data["number"])
        ocr_results["certainty"] = 1.0  # Set certainty to maximum for calibration
        ocr_results["timestamp"] = timestamp.strftime("%Y-%m-%d %H:%M:%S")

        # Save the updated OCR results
        save_ocr_results(ocr_results)

        return jsonify({"message": "OCR value calibrated successfully", "ocr_results": ocr_results})

    # Get the current server time
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Render a simple HTML form for GET requests
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Calibrate OCR</title>
    </head>
    <body>
        <h1>Calibrate OCR</h1>
        <form id="calibrateForm">
            <label for="number">OCR Number:</label><br>
            <input type="number" id="number" name="number" required><br><br>
            <label for="timestamp">Timestamp (YYYY-MM-DD HH:MM:SS):</label><br>
            <input type="text" id="timestamp" name="timestamp" required placeholder="e.g., {current_time}"><br><br>
            <button type="button" onclick="submitForm()">Submit</button>
        </form>
        <p id="response"></p>
        <script>
            async function submitForm() {{
                const number = document.getElementById('number').value;
                const timestamp = document.getElementById('timestamp').value;
                const responseElement = document.getElementById('response');

                try {{
                    const response = await fetch('/calibrate', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ number, timestamp }})
                    }});

                    const result = await response.json();
                    if (response.ok) {{
                        responseElement.textContent = 'Success: ' + JSON.stringify(result);
                    }} else {{
                        responseElement.textContent = 'Error: ' + JSON.stringify(result);
                    }}
                }} catch (error) {{
                    responseElement.textContent = 'Error: ' + error.message;
                }}
            }}
        </script>
    </body>
    </html>
    '''


@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=DEBUG_MODE)
