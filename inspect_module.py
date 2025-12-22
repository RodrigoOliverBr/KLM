
try:
    import vertexai.preview.generative_models
    print("Generative Models content:")
    print(dir(vertexai.preview.generative_models))
except ImportError:
    print("vertexai.preview.generative_models not found")

try:
    from vertexai.preview.generative_models import GenerativeModel
    print("GenerativeModel imported successfully")
except ImportError:
    print("GenerativeModel not found")
