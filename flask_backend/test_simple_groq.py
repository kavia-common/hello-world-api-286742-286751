import os
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

groq_api_key = os.getenv("GROQ_API_KEY")

client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=groq_api_key
)

filename = "audios/A1dmuS6DOhE.mp3"

if not os.path.exists(filename):
    print(f"Error: File {filename} not found.")
else:
    with open(filename, "rb") as file:
        transcription = client.audio.transcriptions.create(
          file=(filename, file.read()),
          model="whisper-large-v3",
          temperature=0,
          response_format="verbose_json",
        )
        print(transcription.text)