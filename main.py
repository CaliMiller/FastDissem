from fastapi import FastAPI, Form, Depends, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import requests
import tweepy  # Twitter API
import facebook  # Facebook API (requires facebook-sdk)
from atproto import Client  # Bluesky API
import googleapiclient.discovery  # YouTube API
import time
import threading

app = FastAPI()
templates = Jinja2Templates(directory="templates")
