
from vertexai.preview.vision_models import ImageGenerationModel
print(dir(ImageGenerationModel))
try:
    model = ImageGenerationModel.from_pretrained("imagegeneration@006")
    print("Instance Dir:", dir(model))
except:
    pass
