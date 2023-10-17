from flask import Flask, render_template, request, redirect, Response, jsonify
import requests
from models import MatchModel
from Constants import *
from utils import *
import json
import os

app = Flask(__name__)

@app.route("/api/matches/<match_id>", methods=['GET'])
def match_data(match_id: str):
  # if match_id == "c8d8bc50-c030-4abf-a27e-66b1c157429c":
  #   match_data = json.loads(open("example_split_game.json").read())
  req_headers = {"X-Riot-Token": os.environ['riot_api_key']}
  match_data = requests.get(f"https://ap.api.riotgames.com/val/match/v1/matches/{match_id}", headers=req_headers).json()
  match: MatchModel = MatchModel(match_data)
  response = jsonify({"players": match.get_players(), "kill_data": match.duel_data, "map_details": MAP[match.get_map()]})
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response

@app.route("/api/utils/filter", methods=['POST', 'GET'])
def get_filtered_data():
  data = json.loads(request.get_data().decode('utf8'))
  kill_data = data["kill_data"]
  options = data["options"]
  response = jsonify({ "data": filter_data(kill_data, options) })
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response

@app.route("/api/utils/plot", methods=['POST', 'GET'])
def get_plotted_image():
  data = json.loads(request.get_data().decode('utf8'))
  kills = data["data"]
  map_details = data["map_details"]
  response = jsonify( {"data": plot_image(kills, map_details)} )
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response

if __name__ == "__main__":
  app.run(debug=True, use_reloader=True)