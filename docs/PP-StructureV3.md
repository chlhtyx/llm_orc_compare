# Please make sure the requests library is installed
# pip install requests
import base64
import os
import requests

API_URL = "https://0bb8b1gbrfcbt7za.aistudio-app.com/layout-parsing"
TOKEN = "ee5eae1eb3c51f024f3d8a5a164562a17663545b"

file_path = "<local file path>"

with open(file_path, "rb") as file:
    file_bytes = file.read()
    file_data = base64.b64encode(file_bytes).decode("ascii")

headers = {
    "Authorization": f"token {TOKEN}",
    "Content-Type": "application/json"
}

required_payload = {
    "file": file_data,
    "fileType": <file type>,  # For PDF documents, set `fileType` to 0; for images, set `fileType` to 1
}

optional_payload = {
    "markdownIgnoreLabels": [
        "header",
        "header_image",
        "footer",
        "footer_image",
        "number",
        "footnote",
        "aside_text"
    ],
    "useChartRecognition": False,
    "useRegionDetection": True,
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": False,
    "useSealRecognition": True,
    "useFormulaRecognition": True,
    "useTableRecognition": True,
    "layoutThreshold": 0.5,
    "layoutNms": True,
    "layoutUnclipRatio": 1,
    "textDetLimitType": "min",
    "textDetLimitSideLen": 64,
    "textDetThresh": 0.3,
    "textDetBoxThresh": 0.6,
    "textDetUnclipRatio": 1.5,
    "textRecScoreThresh": 0,
    "sealDetLimitType": "min",
    "sealDetLimitSideLen": 736,
    "sealDetThresh": 0.2,
    "sealDetBoxThresh": 0.6,
    "sealDetUnclipRatio": 0.5,
    "sealRecScoreThresh": 0,
    "useTableOrientationClassify": True,
    "useOcrResultsWithTableCells": True,
    "useE2eWiredTableRecModel": False,
    "useE2eWirelessTableRecModel": False,
    "useWiredTableCellsTransToHtml": False,
    "useWirelessTableCellsTransToHtml": False,
    "parseLanguage": "default"
}

payload = {**required_payload, **optional_payload}

response = requests.post(API_URL, json=payload, headers=headers)
print(response.status_code)
assert response.status_code == 200
result = response.json()["result"]

output_dir = "output"
os.makedirs(output_dir, exist_ok=True)

for i, res in enumerate(result["layoutParsingResults"]):
    md_filename = os.path.join(output_dir, f"doc_{i}.md")
    with open(md_filename, "w", encoding="utf-8") as md_file:
        md_file.write(res["markdown"]["text"])
    print(f"Markdown document saved at {md_filename}")
    for img_path, img in res["markdown"]["images"].items():
        full_img_path = os.path.join(output_dir, img_path)
        os.makedirs(os.path.dirname(full_img_path), exist_ok=True)
        img_bytes = requests.get(img).content
        with open(full_img_path, "wb") as img_file:
            img_file.write(img_bytes)
        print(f"Image saved to: {full_img_path}")
    for img_name, img in res["outputImages"].items():
        img_response = requests.get(img)
        if img_response.status_code == 200:
            # Save image to local
            filename = os.path.join(output_dir, f"{img_name}_{i}.jpg")
            with open(filename, "wb") as f:
                f.write(img_response.content)
            print(f"Image saved to: {filename}")
        else:
            print(f"Failed to download image, status code: {img_response.status_code}")