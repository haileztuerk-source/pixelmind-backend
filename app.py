import os, base64, requests, tempfile
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow all origins – needed for mobile browser access

REPLICATE_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")

MODELS = {
    "flux-schnell": "black-forest-labs/flux-schnell",
    "sdxl":         "stability-ai/sdxl:7762fd07cf82c948538e41f63f77d685e02b063e37ec1375d6aee7e2e08fcd84",
    "flux-dev":     "black-forest-labs/flux-dev",
}

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/edit", methods=["POST"])
def edit_image():
    if not REPLICATE_TOKEN:
        return jsonify({"error": "REPLICATE_API_TOKEN not set"}), 500

    data = request.json
    prompt   = data.get("prompt", "")
    image_b64 = data.get("image", "")   # base64 dataURL
    strength  = float(data.get("strength", 0.6))
    model_key = data.get("model", "flux-schnell")

    if not prompt or not image_b64:
        return jsonify({"error": "prompt and image required"}), 400

    # Upload image to a temp host Replicate can reach (file.io – free, no account)
    # Strip data URL header if present
    if "," in image_b64:
        image_b64 = image_b64.split(",")[1]

    image_bytes = base64.b64decode(image_b64)

    # Upload via file.io (free, anonymous, auto-deletes after 1 download)
    upload_resp = requests.post(
        "https://file.io/?expires=1h",
        files={"file": ("image.jpg", image_bytes, "image/jpeg")},
        timeout=30
    )
    if not upload_resp.ok:
        return jsonify({"error": "Image upload failed"}), 500

    image_url = upload_resp.json().get("link")
    if not image_url:
        return jsonify({"error": "No upload link returned"}), 500

    # Call Replicate
    model = MODELS.get(model_key, MODELS["flux-schnell"])

    headers = {
        "Authorization": f"Bearer {REPLICATE_TOKEN}",
        "Content-Type": "application/json",
        "Prefer": "wait",   # wait up to 60s for result (no polling needed)
    }

    # Build input depending on model
    if "flux" in model_key:
        inp = {
            "prompt": prompt,
            "image":  image_url,
            "strength": strength,
            "num_outputs": 1,
            "output_format": "webp",
        }
    else:
        # SDXL img2img
        inp = {
            "prompt": prompt,
            "image":  image_url,
            "prompt_strength": strength,
            "num_outputs": 1,
        }

    resp = requests.post(
        f"https://api.replicate.com/v1/models/{model}/predictions" if "/" in model and ":" not in model
        else f"https://api.replicate.com/v1/predictions",
        json={"input": inp} if "/" in model and ":" not in model else {"version": model.split(":")[1] if ":" in model else None, "input": inp},
        headers=headers,
        timeout=120,
    )

    if not resp.ok:
        return jsonify({"error": f"Replicate error: {resp.text}"}), 500

    result = resp.json()

    # Extract output URL
    output = result.get("output")
    if isinstance(output, list):
        output = output[0]

    if not output:
        return jsonify({"error": "No output from model", "raw": result}), 500

    # Download result and return as base64 (avoids CORS on image URLs)
    img_resp = requests.get(output, timeout=30)
    if not img_resp.ok:
        # Just return the URL directly
        return jsonify({"url": output})

    result_b64 = base64.b64encode(img_resp.content).decode()
    mime = img_resp.headers.get("Content-Type", "image/webp")
    return jsonify({"image": f"data:{mime};base64,{result_b64}"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
