
import sys
print(f"Python: {sys.executable}")

try:
    import vertexai.preview.vision_models
    print("Module vertexai.preview.vision_models found.")
    print("Dir:", dir(vertexai.preview.vision_models))
except ImportError as e:
    print(f"ImportError: {e}")

try:
    from vertexai.preview.vision_models import VideoGenerationModel
    print("SUCCESS: VideoGenerationModel found.")
except ImportError:
    print("FAIL: VideoGenerationModel NOT found.")

try:
    from vertexai.preview.vision_models import ImageGenerationModel
    print("SUCCESS: ImageGenerationModel found.")
except ImportError:
    print("FAIL: ImageGenerationModel NOT found.")
