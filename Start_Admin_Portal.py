import uvicorn
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

if __name__ == "__main__":
    print("Starting Admin Portal on http://localhost:8000")
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000, reload=True)
