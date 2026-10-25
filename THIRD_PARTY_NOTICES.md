# Third-party notices

JoeyLab's own code is released under the [MIT licence](LICENSE). It relies on the models, data, fonts, libraries
and programs below, which keep their own licences. Thank you to everyone who made them.

Only the fonts, Leaflet and Natural Earth's outlines are included in this repository. Models and data are downloaded on first use, to your
own Mac, from their publishers; Python packages are installed from PyPI.

## Models

Downloaded the first time a feature needs them.

| Model | Used for | Licence | Source |
|---|---|---|---|
| SigLIP 2, `ViT-B-16-SigLIP2-384` (Google), through open_clip | search, naming events by what they show | Apache-2.0 | [timm/ViT-B-16-SigLIP2-384](https://huggingface.co/timm/ViT-B-16-SigLIP2-384) |
| CLIP ViT-L/14 (OpenAI), through open_clip | scene grouping, similar photos, taste, aesthetics | MIT | [openai/CLIP](https://github.com/openai/CLIP) |
| LAION improved aesthetic predictor (`sac+logos+ava1-l14-linearMSE`) | the aesthetic part of the quality score | Apache-2.0 | [christophschuhmann/improved-aesthetic-predictor](https://github.com/christophschuhmann/improved-aesthetic-predictor) |
| MediaPipe face landmarker (Google) | closed eyes and smiles | Apache-2.0 | [MediaPipe](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker) |
| InsightFace buffalo_s: SCRFD (`det_500m`) and ArcFace (`w600k_mbf`) | finding and recognising faces, if you choose the most accurate models | **non-commercial research use only** | [deepinsight/insightface](https://github.com/deepinsight/insightface) |
| YuNet (`face_detection_yunet_2023mar`), by Shiqi Yu and contributors, from OpenCV Zoo | finding faces, if you choose the models free for any use | MIT (below) | [opencv/opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet), downloaded from [opencv/face_detection_yunet](https://huggingface.co/opencv/face_detection_yunet) |
| SFace (`face_recognition_sface_2021dec`), by Yaoyao Zhong and contributors, converted to ONNX for OpenCV Zoo | recognising faces, if you choose the models free for any use | Apache-2.0 (below) | [opencv/opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface), downloaded from [opencv/face_recognition_sface](https://huggingface.co/opencv/face_recognition_sface) |

**Face recognition is a choice of two sets of models.** It's off until you turn it on (in the app, or with
`joeylab faces enable`), and you choose its models then; neither is downloaded before that.

* **InsightFace's** are the most accurate. InsightFace makes its pretrained models available for non-commercial
  research purposes only. If your use is commercial (for example, a paid photography business), don't choose them
  unless you have a licence from InsightFace.
* **OpenCV Zoo's YuNet and SFace** may be used for anything, including paid work, under the licences below. OpenCV
  runs them (`cv2.FaceDetectorYN`, `cv2.FaceRecognizerSF`).

Everything else in JoeyLab works without either.

### YuNet: MIT licence

```
MIT License

Copyright (c) 2020 Shiqi Yu <shiqi.yu@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### SFace: Apache License 2.0

SFace is licensed under the Apache License, Version 2.0. The full text is at
<https://www.apache.org/licenses/LICENSE-2.0> and in the model's own folder,
[`models/face_recognition_sface/LICENSE`](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE).
It's provided "as is", without warranties or conditions of any kind. JoeyLab downloads the model file as
published and uses it unchanged.

## Data

| Data | Used for | Licence and attribution |
|---|---|---|
| GeoNames `cities5000`, `admin1CodesASCII`, `countryInfo` | place names, looked up on your Mac | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Attribution: **GeoNames, [geonames.org](https://www.geonames.org)** |
| [Natural Earth](https://www.naturalearthdata.com) 1:50m land and land boundaries, simplified (`src/joeylab/web/static/vendor/naturalearth`) | the map in Places, drawn on your Mac | Public domain. "Made with Natural Earth." |
| OpenStreetMap map tiles | the street map in Places, only when you turn it on | © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright); data under the [ODbL](https://opendatacommons.org/licenses/odbl/). Tiles are used under the [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/) |

The GeoNames files are downloaded unchanged from `download.geonames.org` and read as they are.

## Fonts

Included in `src/joeylab/web/static/fonts` as WOFF2 files of their Latin subset, so no page loads fonts from
elsewhere. All three are under the **SIL Open Font License 1.1**; the full licence text is in
[`src/joeylab/web/static/fonts/OFL.txt`](src/joeylab/web/static/fonts/OFL.txt).

| Font | Copyright |
|---|---|
| Hanken Grotesk | Copyright 2021 The Hanken Grotesk Project Authors |
| JetBrains Mono | Copyright 2020 The JetBrains Mono Project Authors |
| Newsreader | Copyright 2020 The Newsreader Project Authors |

## JavaScript

| Library | Used for | Licence |
|---|---|---|
| [Leaflet](https://leafletjs.com) 1.9.4 | the map in Places | BSD-2-Clause. Copyright (c) 2010-2023, Volodymyr Agafonkin; Copyright (c) 2010-2011, CloudMade |

## Native libraries inside dependency wheels

Some Python packages ship compiled libraries in their wheels. JoeyLab doesn't include or change them: they're
installed from PyPI by uv or pip, as their publishers built them, and loaded as shared libraries.

| Library | Inside | Used for | Licence |
|---|---|---|---|
| [LibRaw](https://www.libraw.org) | `rawpy` | decoding RAW files | LGPL-2.1 or CDDL-1.0 |
| [libheif](https://github.com/strukturag/libheif) (with libde265) | `pi-heif` | reading HEIC and HEIF photos | LGPL-3.0 |
| [FFmpeg](https://ffmpeg.org) | `opencv-python-headless` | a still frame from each video | LGPL-2.1 |

If JoeyLab is ever distributed with these libraries bundled (for example as a Mac app), their LGPL terms apply
to that bundle: their source must be offered and they must remain replaceable.

## Python packages

The main packages JoeyLab depends on, with the licences their package metadata declares:

| Package | Needed for | Licence |
|---|---|---|
| FastAPI | the web app | MIT |
| Starlette | the web app | BSD-3-Clause |
| Uvicorn | the web server | BSD-3-Clause |
| Typer | the command line | MIT |
| Rich | the command line | MIT |
| NumPy | everything numeric | BSD-3-Clause (bundled parts under 0BSD, MIT, Zlib, CC0-1.0) |
| OpenCV (`opencv-python-headless`) | image analysis | Apache-2.0 (bundles FFmpeg, above) |
| Pillow | reading and writing images | MIT-CMU |
| pi-heif | HEIC and HEIF | BSD-3-Clause (bundles libheif, above) |
| rawpy | RAW files | MIT (bundles LibRaw, above) |
| watchdog | noticing new files | Apache-2.0 |
| MediaPipe (`faces` extra) | closed eyes and smiles | Apache-2.0 |
| ONNX Runtime (`faces` extra) | running the face models | MIT |
| PyTorch (`ai` extra) | running the search and CLIP models | BSD-3-Clause (bundled parts under Apache-2.0, BSD-2-Clause, BSL-1.0, MIT) |
| torchvision (`ai` extra) | image transforms | BSD-3-Clause |
| open_clip (`open-clip-torch`, `ai` extra) | loading SigLIP 2 and CLIP | MIT |
| timm (`ai` extra) | model architectures | Apache-2.0 |
| Transformers, tokenizers, safetensors, huggingface_hub (`ai` extra) | text encoders and model downloads | Apache-2.0 |
| PyObjC (`pyobjc-framework-vision`, `mac` extra) | Apple Vision labels and text | MIT |
| pytest, httpx (development only) | the tests | MIT, BSD-3-Clause |

Their own dependencies are under MIT, BSD, Apache-2.0, PSF, MPL-2.0 and similar licences; each package's metadata
states its own.

## External programs

| Program | Used for | Licence |
|---|---|---|
| [exiftool](https://exiftool.org) by Phil Harvey | reading dates, cameras and GPS | the same terms as Perl (the Artistic License or the GPL) |

exiftool isn't included with JoeyLab. You install it yourself (`brew install exiftool`), and JoeyLab runs it as
a separate program.

Tailscale, if you use it, is a separate program and service with its own terms. Apple Vision is part of macOS.
