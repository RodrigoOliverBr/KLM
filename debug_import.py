
import sys
print(f"Python Executable: {sys.executable}")
print(f"Python Version: {sys.version}")

try:
    import google.cloud.aiplatform
    print("SUCCESS: google.cloud.aiplatform imported")
except ImportError as e:
    print(f"ERROR: google.cloud.aiplatform failed: {e}")

try:
    import vertexai
    print("SUCCESS: vertexai imported")
    from vertexai.preview.vision_models import Image as VertexImage, ImageToVideoModel
    print("SUCCESS: vertexai vision models imported")
except ImportError as e:
    print(f"ERROR: vertexai failed: {e}")
except Exception as e:
    print(f"ERROR: vertexai unexpected: {e}")
