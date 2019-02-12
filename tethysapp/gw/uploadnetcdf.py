from __future__ import division
from __future__ import unicode_literals
import os
import json
import netCDF4
import datetime
import numpy as np
import calendar
import subprocess
import tempfile, shutil
import scipy
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import reproject, Resampling
import rasterio
import math
import pygslib
from scipy.optimize import least_squares
import elevation
import csv
from .app import Gw as app
# from ajax_controllers import *

porosity=0.3
#global variables
# thredds_serverpath='/opt/tomcat/content/thredds/public/testdata/groundwater/'
thredds_serverpath = "/home/student/tds/apache-tomcat-8.5.30/content/thredds/public/testdata/groundwater/"

#This function opens the Aquifers.csv file for the specified region and returns a JSON object listing the aquifers
def getaquiferlist(app_workspace,region):
    aquiferlist = []
    aquifercsv = os.path.join(app_workspace.path, region + '/' + region + '_Aquifers.csv')
    with open(aquifercsv) as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            myaquifer = {
                'Id': int(row['ID']),
                'Name': row['Name'],
                'Type': row['Type'],
                'CapsName': row['CapsName'],
                'FieldName':row['NameField']
            }
            if 'Contains' in row:
                if row['Contains'] !="":
                    myaquifer['Contains']=row['Contains'].split('.')
                    myaquifer['Contains']=[int(i) for i in myaquifer['Contains']]
            aquiferlist.append(myaquifer)
    return aquiferlist

#The explode and bbox functions are used to get the bounding box of a geoJSON object
def explode(coords):
    """Explode a GeoJSON geometry's coordinates object and yield coordinate tuples.
    As long as the input is conforming, the type of the geometry doesn't matter."""
    for e in coords:
        if isinstance(e, (float, int, long)):
            yield coords
            break
        else:
            for f in explode(e):
                yield f

def bbox(f):
    x, y = zip(*list(explode(f['geometry']['coordinates'])))
    return round(np.min(x)-.05,1), round(np.min(y)-.05,1), round(np.max(x)+.05,1), round(np.max(y)+.05,1)

def download_DEM(region,myaquifer):
    # Download and Set up the DEM for the aquifer
    app_workspace = app.get_app_workspace()
    name=myaquifer['Name']
    directory = os.path.join(app_workspace.path, region + '/DEM')
    if not os.path.exists(directory):
        os.makedirs(directory)
    minorfile = os.path.join(app_workspace.path, region + '/MinorAquifers.json')
    majorfile = os.path.join(app_workspace.path, region + '/MajorAquifers.json')
    regionfile=os.path.join(app_workspace.path, region + '/'+region+'_State_Boundary.json')
    aquiferShape = {
        'type': 'FeatureCollection',
        'features': []
    }
    fieldname = myaquifer['FieldName']

    if os.path.exists(minorfile):
        with open(minorfile, 'r') as f:
            minor = json.load(f)
        for i in minor['features']:
            if fieldname in i['properties']:
                if i['properties'][fieldname] == myaquifer['CapsName']:
                    aquiferShape['features'].append(i)

    if os.path.exists(majorfile):
        with open(majorfile, 'r') as f:
            major = json.load(f)
        for i in major['features']:
            if fieldname in i['properties']:
                if i['properties'][fieldname] == myaquifer['CapsName']:
                    aquiferShape['features'].append(i)
    if len(aquiferShape['features'])<1:
        with open(regionfile,'r') as f:
            region=json.load(f)
        aquiferShape['features'].append(region['features'][0])
    lonmin, latmin, lonmax, latmax = bbox(aquiferShape['features'][0])
    bounds = (lonmin - .1, latmin - .1, lonmax + .1, latmax + .1)
    dem_path = name.replace(' ', '_') + '_DEM.tif'
    output = os.path.join(directory, dem_path)
    elevation.clip(bounds=bounds, output=output, product='SRTM3')
    print "This step works. 90 m DEM downloaded for ", name
# The following functions are used to automatically fit a variogram to the input data
def great_circle_distance(lon1, lat1, lon2, lat2):
    """Calculate the great circle distance between one or multiple pairs of
    points given in spherical coordinates. Spherical coordinates are expected
    in degrees. Angle definition follows standard longitude/latitude definition.
    This uses the arctan version of the great-circle distance function
    (en.wikipedia.org/wiki/Great-circle_distance) for increased
    numerical stability.
    Parameters
    ----------
    lon1: float scalar or numpy array
        Longitude coordinate(s) of the first element(s) of the point
        pair(s), given in degrees.
    lat1: float scalar or numpy array
        Latitude coordinate(s) of the first element(s) of the point
        pair(s), given in degrees.
    lon2: float scalar or numpy array
        Longitude coordinate(s) of the second element(s) of the point
        pair(s), given in degrees.
    lat2: float scalar or numpy array
        Latitude coordinate(s) of the second element(s) of the point
        pair(s), given in degrees.
    Calculation of distances follows numpy elementwise semantics, so if
    an array of length N is passed, all input parameters need to be
    arrays of length N or scalars.
    Returns
    -------
    distance: float scalar or numpy array
        The great circle distance(s) (in degrees) between the
        given pair(s) of points.
    """
    # Convert to radians:
    lat1 = np.array(lat1)*np.pi/180.0
    lat2 = np.array(lat2)*np.pi/180.0
    dlon = (lon1-lon2)*np.pi/180.0

    # Evaluate trigonometric functions that need to be evaluated more
    # than once:
    c1 = np.cos(lat1)
    s1 = np.sin(lat1)
    c2 = np.cos(lat2)
    s2 = np.sin(lat2)
    cd = np.cos(dlon)

    # This uses the arctan version of the great-circle distance function
    # from en.wikipedia.org/wiki/Great-circle_distance for increased
    # numerical stability.
    # Formula can be obtained from [2] combining eqns. (14)-(16)
    # for spherical geometry (f=0).

    return 180.0 / np.pi * np.arctan2(np.sqrt((c2*np.sin(dlon))**2 + (c1*s2-s1*c2*cd)**2), s1*s2+c1*c2*cd)

def _variogram_residuals(params, x, y, variogram_function, weight):
    """Function used in variogram model estimation. Returns residuals between
    calculated variogram and actual data (lags/semivariance).
    Called by _calculate_variogram_model.
    Parameters
    ----------
    params: list or 1D array
        parameters for calculating the model variogram
    x: ndarray
        lags (distances) at which to evaluate the model variogram
    y: ndarray
        experimental semivariances at the specified lags
    variogram_function: callable
        the actual funtion that evaluates the model variogram
    weight: bool
        flag for implementing the crude weighting routine, used in order to
        fit smaller lags better
    Returns
    -------
    resid: 1d array
        residuals, dimension same as y
    """

    # this crude weighting routine can be used to better fit the model
    # variogram to the experimental variogram at smaller lags...
    # the weights are calculated from a logistic function, so weights at small
    # lags are ~1 and weights at the longest lags are ~0;
    # the center of the logistic weighting is hard-coded to be at 70% of the
    # distance from the shortest lag to the largest lag
    if weight:
        drange = np.amax(x) - np.amin(x)
        k = 2.1972 / (0.1 * drange)
        x0 = 0.7 * drange + np.amin(x)
        weights = 1. / (1. + np.exp(-k * (x0 - x)))
        weights /= np.sum(weights)
        resid = (variogram_function(params, x) - y) * weights
    else:
        resid = variogram_function(params, x) - y

    return resid

def spherical_variogram_model(m, d):
    """Spherical model, m is [psill, range, nugget]"""
    psill = float(m[0])
    range_ = float(m[1])
    nugget = float(m[2])
    return np.piecewise(d, [d <= range_, d > range_],
                        [lambda x: psill * ((3.*x)/(2.*range_) - (x**3.)/(2.*range_**3.)) + nugget, psill + nugget])

'''The generate_variogram function automatically fits a variogram to the data
    Inputs:
        X: a 2d array of geographical coordinates of sample points (longitude, latitude) of length n
        y: an array of length n containing the values at sample points, ordered the same as X
        variogram_function: a function for the variogram model (Spherical, Gaussian)
    Returns:
        variogram_model_parameters: a list of 1. the sill, 2. the range, 3. the nugget'''
def generate_variogram(X,y,variogram_function):
    # This calculates the pairwise geographic distance and variance between pairs of points
    x1, x2 = np.meshgrid(X[:, 0], X[:, 0], sparse=True)
    y1, y2 = np.meshgrid(X[:, 1], X[:, 1], sparse=True)
    z1, z2 = np.meshgrid(y, y, sparse=True)
    d = great_circle_distance(x1, y1, x2, y2)
    g = 0.5 * (z1 - z2) ** 2.
    indices = np.indices(d.shape)
    d = d[(indices[0, :, :] > indices[1, :, :])]
    g = g[(indices[0, :, :] > indices[1, :, :])]

    # Now we will sort the d and g into bins
    nlags = 10
    weight = False
    dmax = np.amin(d) + (np.amax(d) - np.amin(d)) / 2.0
    # dmax = np.amax(d)

    dmin = np.amin(d)
    dd = (dmax - dmin) / nlags
    bins = [dmin + n * dd for n in range(nlags)]
    dmax += 0.001
    bins.append(dmax)

    lags = np.zeros(nlags)
    semivariance = np.zeros(nlags)

    for n in range(nlags):
        # This 'if... else...' statement ensures that there are data
        # in the bin so that numpy can actually find the mean. If we
        # don't test this first, then Python kicks out an annoying warning
        # message when there is an empty bin and we try to calculate the mean.
        if d[(d >= bins[n]) & (d < bins[n + 1])].size > 0:
            lags[n] = np.mean(d[(d >= bins[n]) & (d < bins[n + 1])])
            semivariance[n] = np.mean(g[(d >= bins[n]) & (d < bins[n + 1])])
        else:
            lags[n] = np.nan
            semivariance[n] = np.nan
    lags = lags[~np.isnan(semivariance)]
    semivariance = semivariance[~np.isnan(semivariance)]
    print len(X)
    print lags

    # First entry is the sill, then the range, then the nugget
    if len(lags)>3:
        x0 = [np.amax(semivariance) - np.amin(semivariance), lags[2], 0]
        bnds = ([0., lags[2], 0.], [10. * np.amax(semivariance), np.amax(lags), 1])
    elif len(lags)>1:
        x0 = [np.amax(semivariance) - np.amin(semivariance), lags[0], 0]
        bnds = ([0., lags[0], 0.], [10. * np.amax(semivariance), np.amax(lags), 1])
    else:
        x0 = [0, 0, 0]
        bnds = ([0., 0, 0.], [1000, 10, 1])

    # use 'soft' L1-norm minimization in order to buffer against
    # potential outliers (weird/skewed points)
    res = least_squares(_variogram_residuals, x0, bounds=bnds, loss='soft_l1',
                        args=(lags, semivariance, variogram_function, weight))
    variogram_model_parameters = res.x
    print variogram_model_parameters
    return variogram_model_parameters

def upload_netcdf(points,name,app_workspace,aquifer_number,region,interpolation_type,start_date,end_date,interval,resolution, min_samples, min_ratio, time_tolerance, date_name, make_default, units):
    # Execute the following code to interpolate groundwater levels and create a netCDF File and upload it to the server
    # Download and Set up the DEM for the aquifer
    spots = []
    lons = []
    lats = []
    values = []
    elevations = []
    heights=[]
    aquifermin=points['aquifermin']
    iterations=int((end_date-start_date)/interval+1)
    start_time=calendar.timegm(datetime.datetime(start_date, 1, 1).timetuple())
    end_time=calendar.timegm(datetime.datetime(end_date, 1, 1).timetuple())
    sixmonths=False
    if interval==.5:
        sixmonths=True
        iterations+=1
    #old method of interpolation that uses all data
    if min_samples==1 and min_ratio==0:
        for v in range(0, iterations):
            if sixmonths==False:
                targetyear = int(start_date + interval * v)
                target_time = calendar.timegm(datetime.datetime(targetyear, 1, 1).timetuple())
            else:
                monthyear=start_date+interval*v
                doubleyear=monthyear*2
                if doubleyear%2==0:
                    targetyear=int(monthyear)
                    target_time = calendar.timegm(datetime.datetime(targetyear, 1, 1).timetuple())
                else:
                    targetyear=int(monthyear-.5)
                    target_time = calendar.timegm(datetime.datetime(targetyear, 7, 1).timetuple())
            fiveyears = 157766400 * 2
            myspots = []
            mylons = []
            mylats = []
            myvalues = []
            myelevations = []
            myheights=[]
            slope = 0
            number = 0
            timevalue = 0

            for i in points['features']:
                if 'TsTime' in i and 'LandElev' in i['properties']:
                    if i['properties']['LandElev']==-9999:
                        i['properties']['LandElev']=0
                    tlocation = 0
                    stop_location = 0
                    listlength = len(i['TsTime'])
                    for j in range(0, listlength):
                        if i['TsTime'][j] >= target_time and stop_location == 0:
                            tlocation = j
                            stop_location = 1

                    # target time is larger than max date
                    if tlocation == 0 and stop_location == 0:
                        tlocation = -999

                    # target time is smaller than min date
                    if tlocation == 0 and stop_location == 1:
                        tlocation = -888

                    # for the case where the target time is in the middle
                    if tlocation > 0:
                        timedelta = target_time - i['TsTime'][tlocation - 1]
                        slope = (i['TsValue'][tlocation] - i['TsValue'][tlocation - 1]) / (
                                i['TsTime'][tlocation] - i['TsTime'][tlocation - 1])
                        timevalue = i['TsValue'][tlocation - 1] + slope * timedelta

                    # for the case where the target time is before
                    if tlocation == -888:
                        timedelta = i['TsTime'][0] - target_time
                        if abs(timedelta) > fiveyears:
                            timevalue = 9999
                        elif listlength > 1:
                            if (i['TsTime'][1] - i['TsTime'][0]) != 0:
                                slope = (i['TsValue'][1] - i['TsValue'][0]) / (i['TsTime'][1] - i['TsTime'][0])
                                if abs(slope) > (1.0 / (24 * 60 * 60)):
                                    timevalue = i['TsValue'][0]
                                else:
                                    timevalue = i['TsValue'][0] - slope * timedelta
                                if (timevalue > 0 and timevalue != 9999) or timevalue < aquifermin:
                                    timevalue = i['TsValue'][0]
                            else:
                                timevalue = i['TsValue'][0]
                        else:
                            timevalue = i['TsValue'][0]

                    # for the case where the target time is after
                    if tlocation == -999:
                        timedelta = target_time - i['TsTime'][listlength - 1]
                        if abs(timedelta) > fiveyears:

                            timevalue = 9999
                        elif listlength > 1:
                            if (i['TsTime'][listlength - 1] - i['TsTime'][listlength - 2]) != 0:
                                slope = (i['TsValue'][listlength - 1] - i['TsValue'][listlength - 2]) / (
                                        i['TsTime'][listlength - 1] - i['TsTime'][listlength - 2])
                                if abs(slope) > (1.0 / (24 * 60 * 60)):
                                    timevalue = i['TsValue'][listlength - 1]
                                else:
                                    timevalue = i['TsValue'][listlength - 1] + slope * timedelta
                                if (timevalue > 0 and timevalue != 9999) or timevalue < aquifermin:
                                    timevalue = i['TsValue'][listlength - 1]
                            else:
                                timevalue = i['TsValue'][listlength - 1]
                        else:
                            timevalue = i['TsValue'][listlength - 1]
                    if timevalue != 9999:
                        the_elevation = i['properties']['LandElev'] + timevalue
                        myelevations.append(the_elevation)
                        myvalues.append(timevalue)
                        myspots.append(i['geometry']['coordinates'])
                        mylons.append(i['geometry']['coordinates'][0])
                        mylats.append(i['geometry']['coordinates'][1])
            values.append(myvalues)
            elevations.append(myelevations)
            spots.append(myspots)
            lons.append(mylons)
            lats.append(mylats)
            print len(myvalues)
        lons = np.array(lons)
        lats = np.array(lats)
        values = np.array(values)
        elevations = np.array(elevations)

    #New method for interpolation that uses least squares fit and filters data
    else:
        for v in range(0, iterations):
            if sixmonths==False:
                targetyear = int(start_date + interval * v)
                target_time = calendar.timegm(datetime.datetime(targetyear, 1, 1).timetuple())
            else:
                monthyear=start_date+interval*v
                doubleyear=monthyear*2
                if doubleyear%2==0:
                    targetyear=int(monthyear)
                    target_time = calendar.timegm(datetime.datetime(targetyear, 1, 1).timetuple())
                else:
                    targetyear=int(monthyear-.5)
                    target_time = calendar.timegm(datetime.datetime(targetyear, 7, 1).timetuple())
            fiveyears = (157766400/5)*time_tolerance
            oneyear=(157766400/5)
            myspots = []
            mylons = []
            mylats = []
            myvalues = []
            myelevations = []
            myheights=[]
            timevalue = 0


            for i in points['features']:
                if 'TsTime' in i and 'LandElev' in i['properties'] and ('Outlier' not in i['properties'] or i['properties']['Outlier']==False):
                    if i['properties']['LandElev']==-9999:
                        i['properties']['LandElev']=0
                    listlength = len(i['TsTime'])
                    length_time = end_time - start_time
                    mylength_time = min(i['TsTime'][listlength - 1] - i['TsTime'][0], i['TsTime'][listlength - 1] - start_time, end_time-i['TsTime'][0])

                    ratio = float(mylength_time / length_time)
                    if ratio > min_ratio:
                        tlocation = 0
                        stop_location = 0
                        for j in range(0, listlength):
                            if i['TsTime'][j] >= target_time and stop_location == 0:
                                tlocation = j
                                stop_location = 1

                        # target time is larger than max date
                        if tlocation == 0 and stop_location == 0:
                            tlocation = -999

                        # target time is smaller than min date
                        if tlocation == 0 and stop_location == 1:
                            tlocation = -888

                        # for the case where the target time is in the middle
                        if tlocation > 0:
                            if listlength > min_samples and listlength > 1:
                                y_data = np.array(i['TsValue'])
                                x_data = np.array(i['TsTime'])
                                timevalue=scipy.interpolate.pchip_interpolate(x_data,y_data,target_time)
                                # timedelta = target_time - i['TsTime'][tlocation - 1]
                                # slope = (i['TsValue'][tlocation] - i['TsValue'][tlocation - 1]) / (
                                #         i['TsTime'][tlocation] - i['TsTime'][tlocation - 1])
                                # timevalue = i['TsValue'][tlocation - 1] + slope * timedelta


                            else:
                                timevalue = 9999

                        # for the case where the target time is before
                        if tlocation == -888:
                            if listlength > min_samples:
                                consistent = False
                                if listlength > 10:
                                    consistent = True
                                    for step in range(0, 6):
                                        timechange = i['TsTime'][step+1] - i['TsTime'][step]
                                        if timechange != 0:
                                            slope = (i['TsValue'][step+1] - i['TsValue'][step]) / timechange
                                        else:
                                            consistent = False
                                            break
                                        consistent_slope=5.0/oneyear #5 ft/year
                                        if abs(slope)>consistent_slope:
                                            consistent=False
                                            break
                                        if (i['TsTime'][step+1] - i['TsTime'][step]) > (oneyear*2):
                                            consistent = False
                                            break
                                if (i['TsTime'][0] - target_time) < fiveyears/2 or (consistent and (i['TsTime'][0]-target_time)<(fiveyears)):
                                    y_data = np.array(i['TsValue'])
                                    x_data = np.array(i['TsTime'])
                                    ymax = np.amax(y_data)
                                    ymin = np.amin(y_data)
                                    yrange = ymax - ymin
                                    toplim = y_data[0] + yrange / 2
                                    botlim = y_data[0] - yrange / 2
                                    #sp1 = UnivariateSpline(x_data, y_data, k=1)
                                    if listlength<2:
                                        slope=0.0
                                    elif (i['TsTime'][1]-i['TsTime'][0])!=0:
                                        slope=float((i['TsValue'][1]-i['TsValue'][0])/(i['TsTime'][1]-i['TsTime'][0]))
                                    else:
                                        slope=0.0
                                    slope_val=i['TsValue'][0]+slope*(target_time-i['TsValue'][0])

                                    average = y_data[0]
                                    timevalue = (slope_val + 4 * average) / 5
                                    if timevalue > toplim:
                                        timevalue = toplim
                                    if timevalue < botlim:
                                        timevalue = botlim
                                else:
                                    timevalue = 9999

                            else:
                                timevalue = 9999
                            # for the case where the target time is after
                        if tlocation == -999:
                            if listlength > min_samples:
                                consistent=False
                                if listlength>10:
                                    consistent=True
                                    for step in range(listlength-1,listlength-6,-1):
                                        timechange=i['TsTime'][step] - i['TsTime'][step-1]
                                        if timechange!=0:
                                            slope = (i['TsValue'][step] - i['TsValue'][step-1]) / timechange
                                        else:
                                            consistent=False
                                            break
                                        consistent_slope = 5.0 / oneyear  # 5 ft/year
                                        if abs(slope) > consistent_slope:
                                            consistent = False
                                            break
                                        if (i['TsTime'][step]-i['TsTime'][step-1])>(oneyear*2):
                                            consistent=False
                                            break
                                if (target_time - i['TsTime'][listlength - 1])<(oneyear/2):
                                    timevalue=i['TsValue'][listlength-1]
                                elif (target_time - i['TsTime'][listlength - 1]) < fiveyears/2 or (consistent and (target_time - i['TsTime'][listlength - 1]) < fiveyears):
                                    y_data = np.array(i['TsValue'])
                                    x_data = np.array(i['TsTime'])
                                    ymax = np.amax(y_data)
                                    ymin = np.amin(y_data)
                                    yrange = ymax - ymin
                                    toplim = y_data[listlength-1] + yrange / 2
                                    botlim = y_data[listlength-1] - yrange / 2
                                    if listlength<2:
                                        slope=0.0
                                    elif (i['TsTime'][listlength-1]-i['TsTime'][listlength-2])!=0:
                                        slope=float((i['TsValue'][listlength-1]-i['TsValue'][listlength-2])/(i['TsTime'][listlength-1]-i['TsTime'][listlength-2]))
                                    else:
                                        slope=0.0
                                    slope_val=i['TsValue'][listlength-1]+slope*(target_time-i['TsValue'][listlength-1])

                                    average = y_data[listlength-1]
                                    timevalue = (slope_val + 4 * average) / 5
                                    if timevalue > toplim:
                                        timevalue = toplim
                                    if timevalue < botlim:
                                        timevalue = botlim
                                else:
                                    timevalue = 9999
                            else:
                                timevalue = 9999
                        if timevalue != 9999:
                            the_elevation = i['properties']['LandElev'] + timevalue
                            myelevations.append(the_elevation)
                            myvalues.append(timevalue)
                            myspots.append(i['geometry']['coordinates'])
                            mylons.append(i['geometry']['coordinates'][0])
                            mylats.append(i['geometry']['coordinates'][1])
                            if 'LandElev' in i['properties']:
                                myheights.append(i['properties']['LandElev'])
            values.append(myvalues)
            elevations.append(myelevations)
            spots.append(myspots)
            lons.append(mylons)
            lats.append(mylats)
            heights.append(myheights)
            print len(myvalues)
        lons = np.array(lons)
        lats = np.array(lats)
        values = np.array(values)
        elevations = np.array(elevations)
        heights=np.array(heights)

    #Now we prepare the data for the generate_variogram function
    coordinates = []
    all_empty=True
    for i in range(0, iterations):
        coordinate = np.array((lons[i], lats[i])).T
        coordinates.append(coordinate)
        if len(coordinate)>1:
            all_empty=False
    if all_empty==True:
        message= "There is not enough data to perform interpolation"
        print message
        return message
    coordinates = np.array(coordinates)
    variogram_function = spherical_variogram_model
    variogram_model_parameters=[]
    for j in range(0,iterations):
        if len(coordinates[j])>2:
            X = coordinates[i]
            y = values[i]
            print X
            print y
            variogram_model_parameters.append(generate_variogram(X,y,variogram_function))
        else:
            variogram_model_parameters.append([0,0,0])

    aquiferlist = getaquiferlist(app_workspace, region)
    for i in aquiferlist:
        if i['Id'] == int(aquifer_number):
            myaquifer = i
    myaquifercaps = myaquifer['CapsName']
    fieldname = myaquifer['FieldName']

    AquiferShape = {
        'type': 'FeatureCollection',
        'features': []
    }

    MajorAquifers = os.path.join(app_workspace.path, region + '/MajorAquifers.json')
    if os.path.exists(MajorAquifers):
        with open(MajorAquifers, 'r') as f:
            major = json.load(f)
        for i in major['features']:
            if fieldname in i['properties']:
                if i['properties'][fieldname] == myaquifercaps:
                    AquiferShape['features'].append(i)

    MinorAquifers = os.path.join(app_workspace.path, region + '/MinorAquifers.json')
    if os.path.exists(MinorAquifers):
        with open(MinorAquifers, 'r') as f:
            minor = json.load(f)
        for i in minor['features']:
            if fieldname in i['properties']:
                if i['properties'][fieldname] == myaquifercaps:
                    AquiferShape['features'].append(i)

    State_Boundary = os.path.join(app_workspace.path, region + '/' + region + '_State_Boundary.json')
    with open(State_Boundary, 'r') as f:
        state = json.load(f)

    if myaquifercaps == region or myaquifercaps == 'NONE' or myaquifercaps.replace(" ","_")==region:
        AquiferShape['features'].append(state['features'][0])
    print myaquifer
    print AquiferShape
    lonmin, latmin, lonmax, latmax = bbox(AquiferShape['features'][0])
    latgrid = np.mgrid[latmin:latmax:resolution]
    longrid = np.mgrid[lonmin:lonmax:resolution]
    latrange = len(latgrid)
    lonrange = len(longrid)
    nx = (lonmax - lonmin) / resolution
    ny = (latmax - latmin) / resolution

    print latrange, lonrange

    bounds = (lonmin, latmin, lonmax, latmax)
    west, south, east, north = bounds
    # Download and Set up the DEM for the aquifer if it does not exist already
    dem_path = os.path.join(app_workspace.path, region + "/DEM/" + name.replace(" ", "_") + "_DEM.tif")
    if not os.path.exists(dem_path):
        download_DEM(region, myaquifer)
    # Reproject DEM to 0.01 degree resolution using rasterio
    dem_raster = rasterio.open(dem_path)
    src_crs = dem_raster.crs
    src_shape = src_height, src_width = dem_raster.shape
    src_transform = from_bounds(west, south, east, north, src_width, src_height)
    source = dem_raster.read(1)
    dst_crs = {'init': 'EPSG:4326'}
    dst_transform = from_origin(lonmin, latmax, resolution, resolution)
    dem_array = np.zeros((latrange, lonrange))
    dem_array[:] = np.nan
    reproject(source,
              dem_array,
              src_transform=src_transform,
              src_crs=src_crs,
              dst_transform=dst_transform,
              dst_crs=dst_crs,
              resampling=Resampling.bilinear)
    dem_array = np.array(dem_array)
    dem_array = np.flipud(dem_array)
    dem = np.reshape(dem_array.T, ((lonrange) * latrange))
    if units=="English":
        dem=dem*3.28084 #use this to convert from meters to feet
    dem_grid = np.reshape(dem, (lonrange, latrange))

    outx = np.repeat(longrid, latrange)
    outy = np.tile(latgrid, lonrange)
    depth_grids = []
    elev_grids = []

    for i in range(0, iterations):
        searchradius = 3
        ndmax = len(elevations[i])
        ndmin = max(ndmax - 2,0)
        noct = 0
        nugget = 0
        sill = variogram_model_parameters[i][0]
        vrange = variogram_model_parameters[i][1]
        if len(lons[i])>2:
            params = {
                'x': lons[i],
                'y': lats[i],
                'vr': values[i],
                'nx': nx,
                'ny': ny,
                'nz': 1,
                'xmn': lonmin,
                'ymn': latmin,
                'zmn': 0,
                'xsiz': resolution,
                'ysiz': resolution,
                'zsiz': 1,
                'nxdis': 1,
                'nydis': 1,
                'nzdis': 1,
                'outx': outx,
                'outy': outy,
                'radius': searchradius,
                'radius1': searchradius,
                'radius2': searchradius,
                'ndmax': ndmax,
                'ndmin': ndmin,
                'noct': noct,
                'ktype': 1,
                'idbg': 0,
                'c0': nugget,
                'it': 1,
                'cc': sill,
                'aa': vrange,
                'aa1': vrange,
                'aa2': vrange
            }
            if interpolation_type=="Kriging with External Drift":
                params['vr']=elevations[i]
                params['ve']=heights[i]
                params['outextve']=dem
                params['ktype']=3
            estimate = pygslib.gslib.kt3d(params)
            if interpolation_type=="IDW":
                array=estimate[0]['outidpower']
            else:
                array = estimate[0]['outest']
            depth_grid = np.reshape(array, (lonrange, latrange))
            if interpolation_type == "Kriging with External Drift":
                elev_grid=depth_grid
                depth_grid = elev_grid - dem_grid
            else:
                elev_grid = dem_grid + depth_grid
            depth_grids.append(depth_grid)
            elev_grids.append(elev_grid)
            print i
        else:
            depth_grid=np.full((lonrange,latrange),-9999)
            elev_grid=depth_grid
            depth_grids.append(depth_grid)
            elev_grids.append(elev_grid)
    depth_grids = np.array(depth_grids)
    elev_grids = np.array(elev_grids)

    temp_dir=tempfile.mkdtemp()


    myshapefile = os.path.join(temp_dir, "shapefile.json")
    with open(myshapefile, 'w') as outfile:
        json.dump(AquiferShape, outfile)
    #end if statement

    latlen = len(latgrid)
    lonlen = len(longrid)

    # name=name.replace(' ','_')
    # name=name+'.nc'
    # filename = name
    myunit="m"
    volunit = "Cubic Meters"
    if units=="English":
        myunit="ft"
        volunit="Acre-ft"

    filename=date_name+".nc"
    nc_file = os.path.join(temp_dir, filename)
    h = netCDF4.Dataset(nc_file, 'w', format="NETCDF4")

    #Global Attributes
    h.start_date=start_date
    h.end_date=end_date
    h.interval=interval
    h.resolution=resolution
    h.min_samples=min_samples
    h.min_ratio=min_ratio
    h.time_tolerance=time_tolerance
    h.default=make_default
    h.interpolation=interpolation_type
    h.units=units

    time = h.createDimension("time", 0)
    lat = h.createDimension("lat", latlen)
    lon = h.createDimension("lon", lonlen)
    latitude = h.createVariable("lat", np.float64, ("lat"))
    longitude = h.createVariable("lon", np.float64, ("lon"))
    time = h.createVariable("time", np.float64, ("time"), fill_value="NaN")
    depth = h.createVariable("depth", np.float64, ('time', 'lon', 'lat'), fill_value=-9999)
    elevation = h.createVariable("elevation", np.float64, ('time', 'lon', 'lat'), fill_value=-9999)
    elevation.long_name = "Elevation of Water Table"
    elevation.units = myunit
    elevation.grid_mapping = "WGS84"
    elevation.cell_measures = "area: area"
    elevation.coordinates = "time lat lon"

    depth.long_name = "Depth to Water Table"
    depth.units = myunit
    depth.grid_mapping = "WGS84"
    depth.cell_measures = "area: area"
    depth.coordinates = "time lat lon"

    drawdown = h.createVariable("drawdown", np.float64, ('time', 'lon', 'lat'), fill_value=-9999)
    drawdown.long_name = "Well Drawdown Since "+str(start_date)
    drawdown.units = myunit
    drawdown.grid_mapping = "WGS84"
    drawdown.cell_measures = "area: area"
    drawdown.coordinates = "time lat lon"

    volume = h.createVariable("volume", np.float64, ('time', 'lon', 'lat'), fill_value=-9999)
    volume.long_name = "Change in aquifer storage volume since " + str(start_date)
    volume.units = volunit
    volume.grid_mapping = "WGS84"
    volume.cell_measures = "area: area"
    volume.coordinates = "time lat lon"

    latitude.long_name = "Latitude"
    latitude.units = "degrees_north"
    latitude.axis = "Y"
    longitude.long_name = "Longitude"
    longitude.units = "degrees_east"
    longitude.axis = "X"
    time.axis = "T"
    time.units = 'days since 0001-01-01 00:00:00 UTC'
    latitude[:] = latgrid[:]
    longitude[:] = longrid[:]
    year = start_date
    timearray = []  # [datetime.datetime(2000,1,1).toordinal()-1,datetime.datetime(2002,1,1).toordinal()-1]
    t=0
    for i in range(0, iterations):
        a = lons[i]
        b = lats[i]
        c = values[i]
        d = elevations[i]
        a = np.array(a)
        b = np.array(b)
        c = np.array(c)
        d = np.array(d)
        interpolatable=True
        if len(c)<3 or len(d) <3:
            interpolatable=False

        if sixmonths == False:
            year = int(year)
            timearray.append(datetime.datetime(year, 1, 1).toordinal())
        else:
            monthyear = start_date + interval * i
            doubleyear = monthyear * 2
            if doubleyear % 2 == 0:
                monthyear = int(monthyear)
                timearray.append(datetime.datetime(monthyear, 1, 1).toordinal())
            else:
                monthyear = int(monthyear - .5)
                timearray.append(datetime.datetime(monthyear, 7, 1).toordinal())
        year += interval

        if interpolatable!=False: # for IDW, Kriging, and Kriging with External Drift
            time[t] = timearray[i]
            for y in range(0, latrange):
                depth[t, :, y] = depth_grids[i, :, y]
                elevation[t, :, y] = elev_grids[i, :, y]
                if t == 0:
                    drawdown[t, :, y] = 0
                else:
                    drawdown[t, :, y] = depth[t, :, y] - depth[0, :, y]
                mylatmin = math.radians(latitude[y] - resolution / 2)
                mylatmax = math.radians(latitude[y] + resolution / 2)
                area = 6371000 * math.radians(resolution) * 6371000 * abs(
                    (math.sin(mylatmin) - math.sin(mylatmax)))  # 3959 is the radius of the earth in miles, 6,371,000 is radius in meters
                if units=="English":
                    area = 3959 * math.radians(resolution) * 3959 * abs(
                        (math.sin(mylatmin) - math.sin(mylatmax)))
                    area = area * 640  # convert from square miles to acres by multiplying by 640
                volume[t, :, y] = drawdown[t, :, y] * porosity * area
            t+=1

    h.close()

    # Calls a shellscript that uses NCO to clip the NetCDF File created above to aquifer boundaries
    myshell = 'aquifersubset.sh'
    directory = temp_dir
    shellscript = os.path.join(app_workspace.path, myshell)
    subprocess.call([shellscript, filename, directory, interpolation_type, region, str(resolution), app_workspace.path])
    return "Success. NetCDF File Created"