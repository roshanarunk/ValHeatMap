import matplotlib.pyplot as plt
import io
import base64

def filter_data(data: list, options: dict = None):
  '''
  complete kill data looks like this
  data: list = [
    {
    // round X
    kill_side: str,
    killer_id: str,
    killer_name: str,
    killer_location: dict,
    victim_id: str,
    victim_name: str,
    victim_location: dict,
    kill_time: int,
    }
  ]


  options looks like this
  options: dict = {
    'team': list, // ex: ['Blue', 'Red']
    'player': list, // ex: [player_id_1, player_id_2]
    'side': list, // ex: ['ATK', 'DEF']
    'time': int, // ex: 10000 (in ms)
    'round': list, // ex: [1, 2, 3, 4, 5]
  }
  '''
  # check if no options are provided
  if options is None:
    result: list = []
    for rnd in data:
      for kill in rnd:
        result.append(kill)
    return result
  # return the kill list as is

  # or the following is run
  result: list = []

  # iterate over the each round
  for idx, rnd in enumerate(data):
    # for each kill in a round
    for kill in rnd:
      # can_take is a flag that will determine if the kill is valid according to options
      can_take: bool = True
      if 'team' in options and options['team'] != [] and kill["killer_team"] not in options['team']:
        if can_take == True: can_take = False
      if 'player' in options and options['player'] != []:
        if kill['killer_id'] not in options['player'] and kill['victim_id'] not in options['player']:
          if can_take == True: can_take = False
      if 'side' in options and options['side'] != [] and kill['kill_side'] not in options['side']:
        if can_take == True: can_take = False
      if 'time' in options and kill['kill_time'] > options['time']:
        if can_take == True: can_take = False
      if 'round' in options and options['round'] != [] and kill['kill_round'] not in options['round']:
        if can_take == True: can_take = False
      
      # check if the flag is still true after all the validation
      if can_take == True:
        result.append(kill)
  return result

def plot_image(kills, map_details, annotation=(0,0)):
  map_multipliers = map_details["multipliers"]
  map_bounding_box = map_details["bounding_box"]

  scale_factor :dict = map_multipliers

  annotate_killer: bool = annotation[0]
  annotate_victim: bool = annotation[1]

  offsets: dict = { "x": 0, "y": 0 }

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

      # killer_color: str = 'green' if kill["killer_team"] == 'Blue' else 'red'
      # victim_color: str = 'red' if kill["killer_team"] == 'Blue' else 'green'
      killer_color: str = 'green'
      victim_color: str = 'red'
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
  plt.axis('off')
  s = io.BytesIO()
  plt.savefig(s, format="svg")
  plt.close()
  s = base64.b64encode(s.getvalue()).decode("utf-8").replace("\n", "")
  # # plt.savefig('plot.svg')
  # return "data:image/png;base64,%s" % s
  return s