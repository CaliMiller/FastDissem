#!/bin/bash

# Update system and install dependencies
echo "Updating system..."
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv

# Create project directory
echo "Setting up project directory..."
mkdir -p social-media-poster/templates
cd social-media-poster

# Set up Python virtual environment
echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install required Python packages
echo "Installing dependencies..."
pip install fastapi uvicorn requests tweepy facebook-sdk atproto google-api-python-client jinja2

# Ensure templates directory exists
echo "Setting up templates..."
mv ../index.html templates/

# Run the FastAPI server
echo "Starting FastAPI server..."
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
