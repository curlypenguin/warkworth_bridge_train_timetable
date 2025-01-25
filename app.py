#!/usr/bin/env python3

from datetime import datetime, timedelta
import json
import os
import pytz

from flask import Flask, render_template
from zeep import Client, Settings, xsd
from zeep.plugins import HistoryPlugin
import pandas as pd


def save_train(train,bridge_datetime,station,time,direction,stop_list,station_names,train_data,service_IDs):
    # puts all the information on the train into the train_data dataframe
    bridgeTime = datetime.strftime(bridge_datetime,'%H:%M') # convert time to string
    ukTimezone = pytz.timezone('Europe/London') # get uk timezone
    timeNow = datetime.now(tz=ukTimezone) # get time in UK timezone
    bridge_datetime = ukTimezone.localize(bridge_datetime) # add timezone to bridge_datetime so it can be compared to timeNow
    if bridge_datetime + timedelta(minutes=5) > timeNow and train.serviceID not in service_IDs:
        train_data.loc[len(train_data.index)] = [bridgeTime,train.operator,train.destination.location[0].locationName,
                                               station_names[station],time,stop_list[station][1],direction,bridge_datetime]
        # if London North Eastern Railway, shorten to LNER
        if train_data['Operator'][len(train_data.index)-1] == 'London North Eastern Railway':
            train_data.loc[len(train_data.index)-1,'Operator'] = 'LNER'
    return train_data


def interpolate_time(end_time,start_time,end_distance,start_distance):
    # linear interpolation to find time crossing bridge from start and end times and distances
    endDatetime = datetime.combine(datetime.today(),datetime.strptime(end_time,'%H:%M').time()) # convert to datetime, assuming today
    startDatetime = datetime.combine(datetime.today(),datetime.strptime(start_time,'%H:%M').time())
    if endDatetime < startDatetime:               # if stops different sides of midnight
        endDatetime += timedelta(days=1)    # add one day
    bridge_datetime = (start_distance/(start_distance+end_distance))*(endDatetime-startDatetime) + startDatetime # linear interpolation
    if datetime.today() - bridge_datetime > timedelta(hours=2):    # if bridge time more than 2 hours before current time
        bridge_datetime += timedelta(days=1)                       # assume it's tomorrow - add one day
    return bridge_datetime


def nearest_station(stations,stop_list,distances):
    # finds the nearest station to the bridge in one direction that is included in the train's stop list
    for station in stations:
        if station in stop_list: # searches list of stations in order until it finds one included in the stop list
            distance = float(distances[station])
            time = stop_list[station][0]
            break
    return station,distance,time # returns the station, distance to the station and time stopped at station


def get_stops(train,origin,client,header_value):
    # gets all the stops for a particular train between the start and end stations
    stop_list = {}
    if train.subsequentCallingPoints is not None: # future trains
        # stop for origin
        if train.etd == 'On time':
            stop_list[origin] = [train.std,'Due: On Time']
        elif train.etd == 'Delayed':
            stop_list[origin] = [train.std,'Timetabled: Delayed']
        elif train.etd != 'Cancelled':                  # stop_list empty for cancelled trains
            stop_list[origin] = [train.etd,'Due: Late']
        # future stops
        stops = train.subsequentCallingPoints.callingPointList[0].callingPoint
    else: # past trains
        train_details = client.service.GetServiceDetails(serviceID=train.serviceID, _soapheaders=[header_value])
        # stop for origin
        if train.etd == 'On time':
            stop_list[origin] = [train.std,'Departed: On Time']
        elif train.etd != 'Cancelled':
            stop_list[origin] = [train.etd,'Departed: Late']
        # future stops
        stops = train_details.subsequentCallingPoints.callingPointList[0].callingPoint
    for stop in stops:
        if stop.at is None: # not yet departed stop
            if stop.et == 'On time': # on time
                stop_list[stop.crs] = [stop.st, 'Due: On Time']
            elif stop.et == 'Delayed':
                stop_list[stop.crs] = [stop.st,'Timetabled: Delayed']
            elif stop.et != 'Cancelled': # not cancelled
                stop_list[stop.crs] = [stop.et, 'Due: Late']
        else: # has departed stop
            if stop.at == 'On time':
                stop_list[stop.crs] = [stop.st, 'Departed: On Time']
            elif stop.at != 'Cancelled':
                stop_list[stop.crs] = [stop.at, 'Departed: Late']
    return stop_list


def each_train(direction,services,origin,station_names,distances,south_stations,north_stations,train_data,client,header_value):
    # For each train, find the time it should cross the bridge and print it
    service_IDs = [] # make a list of service IDs to check for duplicates
    for train in services: # repeat for each train in services
        try:
            # get list of all stations stopped at with time stopped
            stop_list = get_stops(train,origin,client,header_value)
            if stop_list != {}:  # check it's not a cancelled train
                # time stopped and distance to nearest station stopped at to south
                southStation, south_distance, south_time = nearest_station(south_stations,stop_list,distances)
                # time and distance to nearest station to north
                northStation, north_distance, north_time = nearest_station(north_stations,stop_list,distances)
                # interpolate based on direction heading then print train
                if direction == 'Northbound':
                    bridge_datetime = interpolate_time(north_time,south_time,north_distance,south_distance)
                    save_train(train,bridge_datetime,southStation,south_time,direction,stop_list,station_names,train_data,service_IDs)
                elif direction == 'Southbound':
                    bridge_datetime = interpolate_time(south_time,north_time,south_distance,north_distance)
                    save_train(train,bridge_datetime,northStation,north_time,direction,stop_list,station_names,train_data,service_IDs)
            service_IDs.append(train.serviceID)
        except:
            print(f"Something went wrong with {train.serviceID}")


def get_services(start,end,client,header_value):
    #gets list of services going between station start and station end
    pastTrains = client.service.GetDepBoardWithDetails(numRows=10, crs=start, filterCrs=end,
                                                       timeOffset=-120, timeWindow = 120, _soapheaders=[header_value]) #past two hours
    futureTrains = client.service.GetDepBoardWithDetails(numRows=10, crs=start, filterCrs=end,
                                                         timeOffset=0, timeWindow = 120, _soapheaders=[header_value]) #next two hours
    services =[]
    if pastTrains.trainServices is not None:
        services += pastTrains.trainServices.service
    if futureTrains.trainServices is not None:
        services += futureTrains.trainServices.service
    return services


def make_dataframe():
    # creates empty dataframe with headers to store train information in
    headers = ['Bridge Time','Operator','Destination','Last Station','Departure Time','On Time?','Direction','Datetime']
    train_data = pd.DataFrame(columns=headers)
    return train_data


def read_stations(filename):
    #reads the csv file, gets:
    station_names = {}   #station names for each 3 letter code
    distances = {}      #distances to each station
    south_stations = []      #list of stations to the south
    north_stations = []      #list of stations to the north
    with open(filename) as file:
        station_data = json.load(file)
        for row in station_data:
            station_names[row["Station"]]=row["Station Name"]
            distances[row["Station"]]=row["Distance"]
            if row["Direction"] == 'S':
                south_stations.append(row["Station"])
            elif row["Direction"] == 'N':
                north_stations.append(row["Station"])
    south_stations.reverse()
    return station_names,distances,south_stations,north_stations


def setup():
    with open('LDBS_key.json') as file:
        API_key = json.load(file)
    LDB_token = API_key["Key"]
    wsdl = 'http://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01'
    settings = Settings(strict=False)
    history = HistoryPlugin()
    client = Client(wsdl=wsdl, settings=settings, plugins=[history])
    header = xsd.Element(
        '{http://thalesgroup.com/RTTI/2013-11-28/Token/types}AccessToken',
        xsd.ComplexType([
            xsd.Element(
                '{http://thalesgroup.com/RTTI/2013-11-28/Token/types}TokenValue',
                xsd.String()),
        ])
    )
    header_value = header(TokenValue=LDB_token)
    return client, header_value


def generate_json_data():
    client, header_value = setup()
    station_names,distances,south_stations,north_stations = read_stations('stations.json')
    southEnd = south_stations[-1]    # stop at the south end
    northEnd = north_stations[-1]    # stop at the north end
    train_data = make_dataframe()
    # Northbound trains
    services = get_services(southEnd,northEnd,client,header_value) # start south, end north
    each_train('Northbound',services,southEnd,station_names,distances,south_stations,north_stations,train_data,client,header_value)
    # Southbound trains
    services = get_services(northEnd,southEnd,client,header_value)
    each_train('Southbound',services,northEnd,station_names,distances,south_stations,north_stations,train_data,client,header_value)
    # sort dataframe by time arriving at bridge
    train_data = train_data.sort_values(by=['Datetime'])

    data = train_data.to_json(orient="records")
    return data


app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')  # HTML template for frontend


@app.route('/api/train-data')
def get_train_data():
    data = generate_json_data()
    return data


def main():
    if os.geteuid() == 0:
        port = 80
    else:
        port = 8080

    app.run(debug=True, port=port, host="0.0.0.0")


if __name__ == '__main__':
    main()

