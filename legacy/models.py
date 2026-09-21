class MatchModel:
  def __init__(self, match_data: dict):
    # check if got valid match data
    if match_data is None:
      raise ValueError("match_data is None")
      return
    # save match data for all future uses
    self.match_raw_data: dict = match_data
    # get all players
    self.all_players: list = self.__get_players()
    # get complete round data
    self.all_round_data: list = self.match_raw_data['roundResults']
    # how many locations are NoneType
    self.none_count: int = 0
    # compile only kills data from all rounds
    self.duel_data: list = self.__compile_round_info()

  def __get_players(self):
    # info of all participated players is in the 'players' key
    data: list = self.match_raw_data['players']
    players: list = []
    for player in data:
      # for every player get their id, name, team and played character
      players.append({"id": player['puuid'], "name": player['gameName'], 'team': player['teamId'], 'character': player['characterId']})
    return players
  
  # makes a kills only data list from the match data
  def __compile_round_info(self):
    round_info: list = self.all_round_data
    kills_by_round: list = []

    # iterate over each round
    for rnd in round_info:
      # print(rnd['roundNum'])
      # print('-'*50)
      kills_in_round: list = []
      # iterate over each player's actions in the round
      player_stats = rnd['playerStats']
      for player in player_stats:
        # if the player has more than 0 kills in the round
        if(len(player["kills"]) > 0):
          # each of their kill will be in the 'kills' key
          kill_data = player["kills"]
          # iterate over singular kill
          for kill in kill_data:
            # get the killer and victim's id
            killer = kill['killer']
            victim = kill['victim']
            # get the killer's name from the player list
            killer_name = [i for i in self.all_players if i['id'] == killer][0]['name']
            # get the victim's name from the player list
            victim_name = [i for i in self.all_players if i['id'] == victim][0]['name']
            # riot does not provide the location of the killer directly, probably due to fog of war, so we try to get it from the all player position list
            # check the position of each member available in the all position list
            # generate a list consisting of the location of only the killer
            killer_location_array = [i for i in kill["playerLocations"] if i['puuid'] == player["puuid"] ]
            # if the list is empty set location to none
            killer_location = None if len(killer_location_array) == 0 else killer_location_array[0]['location']
            # increase the none counter of the match instance
            if killer_location == None: self.none_count += 1
            # get victim location
            victim_location = kill['victimLocation']
            # print(killer, killer_location)
            # print(victim, victim_location)
            # get which team the killer was on
            killer_team: str = [i for i in self.all_players if i["id"] == killer][0]['team']
            # check which side (atk/def) the killer was on
            kill_side: str = 'ATK' if rnd["roundNum"] < 12 else 'DEF'
            # append id + name + location of the killer and victim and the kill time
            kills_in_round.append({'killer_id': killer, 'killer_name': killer_name, 'victim_id': victim, 'victim_name': victim_name, 'killer_location': killer_location, 'victim_location': victim_location, 'kill_time': kill['timeSinceRoundStartMillis'], 'killer_team': killer_team, "kill_side": kill_side, "kill_round": rnd["roundNum"]})
            # print(killer_name, '-->', victim_name, victim_location, 'at', kill['roundTime'])
      kills_by_round.append(kills_in_round)
    return kills_by_round

  def get_players(self):
    # return the instace's player list
    return self.all_players
  
  def get_map(self):
    return self.match_raw_data["matchInfo"]["mapId"]

class Map():
  def __init__(self):
    self.multipliers = {
      "x": 1,
      "y": 1,
    }
    self.bounding_box = {
      "top_left": { "x": 1, "y": 1 },
      "top_right": { "x": 1, "y": 1 },
      "bottom_left": { "x": 1, "y": 1 },
      "bottom_right": { "x": 1, "y": 1 },
    }