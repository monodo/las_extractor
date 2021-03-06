# -*- coding: UTF-8 -*-
# Adapted from c2c raster profile tool
# SITN, 2013

import sys, os, subprocess, csv, math, threading
import geojson
import numpy as np

from pyramid.view import view_config
from pyramid.i18n import TranslationStringFactory

import pyramid.i18n

from shapely.geometry import LineString
import shapefile 
import uuid
from datetime import datetime
from pyramid.response import Response, FileResponse
from zipfile import ZipFile as zip
import pkg_resources
from las_extractor.util.point_cloud_profiler import *
from las_extractor.util.temp_file_manager import remove_old_files

import sys

@view_config(route_name='lidar_profile', renderer='jsonp')
def lidar_profile(request):
    """
        Extract LiDAR point cloud profile from buffered polyline and return json file. Also stores the result
        as .csv and .kml temporary file for export fonctionnality
        Requires FUSION PolyClipData.exe (including dlls) and LASTOOLS las2txt.exe

        Tile selection uses postgis polygon intersection. The postgis tile layer requires a attributes pointing to the tile file name

        Recommended tile size is about 50 meters

        SITN 2013
    """
    _ = request.translate

    # Get resolution settings
    resolution = request.registry.settings['resolution']

    is_windows = False

    if sys.platform == 'win32':
        # use Command
        is_windows = True

    # Get configuration values
    if 'code' in request.params and request.params['code'] == resolution[0]['intranet_code']:
        maxLineDistance = resolution[1]['max_line_distance']
        bufferSizeMeter = resolution[1]['buffer_size']
    else:
        maxLineDistance = resolution[2]['max_line_distance']
        bufferSizeMeter = resolution[2]['buffer_size']

    # limit calculation time to avoid server meltdown...
    maxCalculationTime = request.registry.settings['timeout']

    # required paths 
    outputDir = request.registry.settings['lidar_output_dir'].replace('\\', '/')  
    dataDirStandard = request.registry.settings['lidar_data']
    dataDirNormalized = request.registry.settings['lidar_data_normalized']
    fusionCmd = request.registry.settings['lidar_fusion_cmd']
    lastoolCmd= request.registry.settings['lidar_lastool_cmd'] 
    outputCsv = str(uuid.uuid4()) + '.csv' 
    # global variables
    classesNames = {}
    distanceFromOrigin = 0
    classesList = []  
    jsonOutput=[]
    zMin = []
    zMax = []
    csvOut = open(outputDir + outputCsv, 'w')
    csvOut.write('distance,altitude,x,y,class\n') # csv file header

    # remove files if older than 10 minutes

    errorMessage = remove_old_files(outputDir, 600)
    if errorMessage != '':
        logFile = open(outputDir + 'lock.log','a')
        logFile.write(str(datetime.now()) + ': ' + errorMessage +'\n')
        logFile.close()

    if outputDir == 'overwriteme' or dataDirStandard == 'overwriteme' or dataDirNormalized == 'overwriteme' \
        or fusionCmd == 'overwriteme' or lastoolCmd == 'overwriteme':

        csvOut.close()
        errorMsg = '<b>' + _('ERROR') + ':</b><p>'
        errorMsg +=  _('Paths not defined in buildout for one of the following variables: ')
        errorMsg += 'lidar_fusion_cmd, lidar_lastool_cmd, lidar_data, lidar_data_normalized </p>'
        return {'Warning': errorMsg}

    # Read the profile line posted by the client
    geom = geojson.loads(request.params['geom'], object_hook=geojson.GeoJSON.to_instance)

    # Choose the correct data set and set up the correct variables
    dataType = request.params['dataType'] 
    if dataType == 'standard':
        dataDir = dataDirStandard

        # check if the remote disk is connected
        if not os.path.exists(dataDir):
            csvOut.close()
            errorMsg = '<b>' + _('ERROR') + ':</b><p>'
            errorMsg +=  _('LiDAR data directory not accessible') + '</p>'
            return {'Warning': errorMsg}

    elif dataType == 'normalized':
        dataDir = dataDirNormalized
        # check if the remote disk is connected
        if not os.path.exists(dataDir):
            csvOut.close()
            errorMsg = '<b>' + _('ERROR') + ':</b><p>'
            errorMsg += _('LiDAR data directory not accessible') + '</p>'
            return {'Warning': errorMsg}

    classesNames = request.registry.settings['classes_names_'+dataType]

    # Full line recieved from client: if too long: return error in order to avoid a client's overflow
    fullLine = LineString(geom.coordinates)
    if fullLine.length > maxLineDistance:
        csvOut.close()
        errorMsg = '<b>' + _('WARNING') + '</b>: <p>' + _('The profile you draw is ')
        errorMsg += str(math.ceil(fullLine.length * 1000) / 1000) + 'm ' +_('long') +', '
        errorMsg +=  _('max allowed length is') +': ' + str(maxLineDistance) + 'm </p>'
        return {'Warning': errorMsg}

    # Iterate over line segments
    for i in range(0, len(geom.coordinates) -  1):
        # generate unique names for output filenames
        fileList = 'fileList_' + str(uuid.uuid4()) + '.txt'
        intersectPolygon = 'intersectPolygon_' + str(uuid.uuid4())
        outputLas = 'ouputlas_' + str(uuid.uuid4()) + '.las'
        outputTxt = 'ouputtxt_' + str(uuid.uuid4()) + '.txt'

        # Segment start and end coordinates
        xyStart = geom.coordinates[i]
        xyEnd = geom.coordinates[i + 1]

        # current line segment
        segment = LineString([xyStart, xyEnd])

        # generate the tile list intersected by the buffer around the segment segment
        polygon, checkEmpty = generate_tile_list(segment, bufferSizeMeter, outputDir, fileList, dataDir)

        # If no tile is found din the area intersected by the segment, return empty json
        if checkEmpty == 0:
            csvOut.close()
            errorMsg = '<b>' + _('WARNING') + '</b>: <p>'
            errorMsg +=  _('The profile you draw is entirely outside the data extent') + '</p>'
            return {'Warning': errorMsg}

        # Write the buffer as an ESRI polygon shapfile (required by Fusion PolyClipData.exe)
        write_polygon_shapefile(polygon, outputDir, intersectPolygon)

        # Call FUSION POlyClipData to extract the points that are within the buffer extent
        if is_windows is True:
            polyClipCmd = fusionCmd + ' ' + outputDir + intersectPolygon + '.shp '
            polyClipCmd += outputDir + outputLas + ' ' + outputDir + fileList
            polyClipCmdObject = Command(polyClipCmd)
            polyClipCmdObject.run(timeout = maxCalculationTime)

            # stop process if taking too long
            if polyClipCmdObject.timeTooLong:
                csvOut.close()
                errorMsg = '<b>' + _('ERROR') + '</b>: <p>'
                errorMs +=  _('Extraction process timeout exception') + '</p>'
                return {'Warning': errorMsg}

            # Call LASTOOL las2Txt to export .las file to csv format
            las2TxtCmd = lastoolCmd + ' -i ' + outputDir + outputLas + ' -o '
            las2TxtCmd += outputDir + outputTxt + ' -parse xyzc -sep space'
            las2TxtCmd = las2TxtCmd.replace('/', "\\")
            las2TxtCmdObject = Command(las2TxtCmd)
            las2TxtCmdObject.run(timeout = maxCalculationTime)

            # stop process if taking too long
            if las2TxtCmdObject.timeTooLong:
                csvOut.close()
                errorMsg = '<b>' + _('ERROR') + '</b>: <p>'
                errorMsg += _('Extraction process timeout exception') + '</p>'
                return {'Warning': errorMsg}

        else:
            # Should work on LINUX, MacOSx and co.
            polyClipCmd = fusionCmd + ' ' + outputDir.replace("/","\\\\") + intersectPolygon + '.shp '
            polyClipCmd += outputDir.replace("/","\\\\") + outputLas + ' ' + outputDir.replace("/","\\\\") + fileList

            result1 = subprocess.Popen(
                polyClipCmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            ).communicate()

            las2TxtCmd = lastoolCmd + ' -i ' + outputDir + outputLas
            las2TxtCmd += ' -o ' + outputDir + outputTxt + ' -parse xyzc -sep space'
            result2 = subprocess.Popen(
                las2TxtCmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            ).communicate()

        # Arrange the data into numpy array and use fast sorting functionnalities
        profile = generate_numpy_profile(outputDir, outputTxt, xyStart, xyEnd, distanceFromOrigin)

        # increment the distance from the line origin
        distanceFromOrigin += segment.length

        # store segment min/max z value
        zMin.append(np.min(profile[:,1]))
        zMax.append(np.max(profile[:,1]))

        # Read the numpy data and append them to json-serializable list
        generate_json(profile, jsonOutput, csvOut, classesList, classesNames)

        # remove temporary files 
        remove_temp_files(outputDir, fileList, intersectPolygon, outputLas, outputTxt)

    lineZMin = np.min(np.array(zMin))
    lineZMax = np.max(np.array(zMax))

    csvOut.close()

    return {
        'profile': jsonOutput,
        'series':classesList,
        'csvId': outputCsv,
        'zRange': {
            'zMin':lineZMin,
            'zMax':lineZMax
        }
    }

@view_config(route_name='lidar_csv')
def lidar_csv(request):
    """
        Read the csv file stored at profile creation time, and return it
    """
    outputDir = request.registry.settings['lidar_output_dir'].replace('\\','/') 
    csvFileId = request.params['csvFileUID']
    return FileResponse(outputDir + csvFileId, request = request, content_type = 'text/csv; charset=utf-8')

@view_config(route_name='lidar_kml')
def lidar_kml(request):
    """
        Read the csv file stored on disk and transform it to kml
    """

    markerUrl = request.static_url('las_extractor:static/images/googleearthview/')
    outputDir = request.registry.settings['lidar_output_dir'].replace('\\', '/') 
    csvFileId = request.params['csvFileUID']

    classesNames = request.registry.settings['classes_names_'+request.params['dataType']]

    csvData = open(outputDir+csvFileId)
    outputKml = outputDir + str(uuid.uuid4()) + '.kml'

    is_generated = csv2kml(
        csvData,
        markerUrl,
        outputKml,
        classesNames,
        request.registry.settings['kml_colors']
    )

    if is_generated is False:
        strResult = [
           u'<?xml version="1.0" encoding="UTF-8"?>',
            u'<kml xmlns="http://www.opengis.net/kml/2.2">',
            u'<Document><Placemark><name>Point</name>',
            u'<description>Ce point est simulé car le serveur ne supporte pas la',
            u' création de fichier KML.</description>',
            u'<Point><coordinates>6.86835,46.90513,0</coordinates>',
            u'</Point></Placemark></Document></kml>'
        ]
        strResult = ''.join(strResult)
    else:
        data = open(outputKml)
        strResult = ''
        for row in data:
            strResult+= str(row)
        csvData.close() 
    return Response(strResult, headers={
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename="lidarprofil.kml"'
    })

@view_config(route_name='lidar_shp')
def lidar_shp(request):
    """
        Transform the profile line (2D) to ESRI shapefile
    """

    # set up paths
    geom = geojson.loads(request.params['geom'], object_hook =  geojson.GeoJSON.to_instance)
    outputDir = request.registry.settings['lidar_output_dir'].replace('\\', '/') 
    outputShp= outputDir + str(uuid.uuid4())

    # Create pyshp polyline ESRI shapfile and write it to disk
    shapeParts = []
    outShp = shapefile.Writer(shapefile.POLYLINE)
    outShp.line(parts=[geom.coordinates])
    outShp.field('FIRST_FLD','C','40')
    outShp.record('First','Line')
    outShp.save(outputShp)

    # zip the shapefile for nice single output
    zipShp = zip(outputShp +'.zip', mode='w')
    zipShp.write(outputShp + '.shp', os.path.basename(outputShp + '.shp'))
    zipShp.write(outputShp + '.dbf', os.path.basename(outputShp + '.dbf'))
    zipShp.write(outputShp + '.shx', os.path.basename(outputShp + '.shx'))
    zipShp.close()

    # remove files
    os.remove(outputShp + '.shx')
    os.remove(outputShp + '.shp')
    os.remove(outputShp + '.dbf')

    return FileResponse(outputShp + '.zip', request = request, content_type = 'application/zip')

# Helping functions to kill suprocesses when time taken is too long

class Command(object):
    def __init__(self, cmd):
        self.cmd = cmd
        self.process = None
        self.timeTooLong = False

    def run(self, timeout):
        def target():
            self.process = subprocess.Popen(self.cmd)
            self.process.communicate()

        thread = threading.Thread(target = target)
        thread.start()

        thread.join(timeout)
        if thread.is_alive():
            self.process.kill()
            thread.join()
            self.timeTooLong = True
