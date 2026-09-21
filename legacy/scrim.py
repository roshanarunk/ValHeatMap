import json
from models import MatchModel, Map
from Constants import *
from utils import *
import matplotlib.pyplot as plt

if __name__ == '__main__':
  data: dict = {}
  with open('example_icebox_game.json', 'r') as f:
    data = json.loads(f.read())
  
  # bounding boxes,
  # -806, 2400 top left
  # 1767, 2400 top right
  # -806, -256 bottom left
  # 1767, -256 bottom right
  
  match: MatchModel = MatchModel(data)
  players: list = match.get_players()
  kill_data: list = match.duel_data
  map_name: str = match.get_map()

  map_details = MAP[map_name]
  map_multipliers = map_details["multipliers"]
  map_bounding_box = map_details["bounding_box"]

  scale_factor :dict = map_multipliers

  annotate_killer: bool = False
  annotate_victim: bool = False

  offsets: dict = { "x": 0, "y": 0 }

  kills = filter_data(kill_data, {
    'side': ['ATK']
  })
  print(len(kills))
  for kill in kills:
    killer_loc = kill['killer_location']
    victim_loc = kill['victim_location']
    # check if any of the locations are NoneType
    if killer_loc is not None and victim_loc is not None:
      # get the coordinates of the locations and downscale them
      killer_x: float = (killer_loc["x"]*scale_factor["x"] + offsets["x"])
      killer_y: float = (killer_loc["y"]*scale_factor["y"] + offsets["y"])
      victim_x: float = (victim_loc["x"]*scale_factor["x"] + offsets["x"])
      victim_y: float = (victim_loc["y"]*scale_factor["y"] + offsets["y"])

      killer_color: str = 'green' if kill["killer_team"] == 'Blue' else 'red'
      victim_color: str = 'red' if kill["killer_team"] == 'Blue' else 'green'
      # first plot line between markers
      plt.plot([killer_x, victim_x],[killer_y, victim_y], color='#aaaaaa', linewidth=1)
      # then plot killer marker
      plt.plot(killer_x, killer_y, color=killer_color, marker='o', markersize=2)
      # annotate killer data
      if annotate_killer is True:
        plt.annotate(kill['killer_name'], xy=(killer_x, killer_y), xytext=(killer_x + 10, killer_y + 10), color=killer_color, size=3)
      # then plot victim marker
      plt.plot(victim_x, victim_y, color=victim_color, marker='.', markersize=1)
  
  #plot bounding box points
  plt.plot([ map_bounding_box["top_left"]["x"], map_bounding_box["top_right"]["x"], map_bounding_box["bottom_right"]["x"], map_bounding_box["bottom_left"]["x"] ],[ map_bounding_box["top_left"]["y"], map_bounding_box["top_right"]["y"], map_bounding_box["bottom_right"]["y"], map_bounding_box["bottom_left"]["y"] ], color="black", linewidth=0)
  # plt.gca().invert_xaxis()
  # plt.gca().invert_yaxis()
  plt.axis('off')
  # plt.figure(figsize=(2,2))
  # plt.show()
  # mplcursors.cursor(hover=True)
  # img = plt.imread('bonsai.png')
  # plt.imshow(img)
  plt.show()
  plt.savefig('plot.svg')