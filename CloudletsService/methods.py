import math
import grpc
import pymongo.errors
from pymongo import MongoClient
from bson import objectid, errors
from google.protobuf.json_format import MessageToDict, ParseDict
from CloudletsService import cloudlets_service_pb2, cloudlets_service_pb2_grpc


class Point:
    def __init__(self, latitude, longitude):
        self.latitude = latitude
        self.longitude = longitude

    def __repr__(self):
        return f"Point({self.latitude} x {self.longitude})"


def calculate_dist(from_x, from_y, to_x, to_y):
    return math.sqrt((from_x - to_x) ** 2 + (from_y - to_y) ** 2)


class CloudletsAPI(cloudlets_service_pb2_grpc.CloudletsAPIServicer):
    mongo_client = MongoClient('mongodb://cloudlets_service:cloudlets_service_pwd@CloudletsServiceDB:27017/cloudlets_service')
    cloudlets_service_db = mongo_client.cloudlets_service
    cloudlets_collection = cloudlets_service_db.cloudlets
    areas_collection = cloudlets_service_db.areas

    scheduling_service_url = 'SchedulingService:50050'

    # Adds a new fog device to the system
    def Add(self, request, context):
        # Check all fog device parameters for emptiness, except id
        for field in request.DESCRIPTOR.fields:
            if not getattr(request, field.name) and field.name != 'id':
                return cloudlets_service_pb2.ResponseWithCloudlet(status=cloudlets_service_pb2.Response(code=422, message='Name, cpu_cores, cpu_frequency, ram_size, rom_size, os, os_kernel, ip, latitude, longitude, country, region, city parameters are required.'))

        object_id = ''
        swarm_address = ''
        swarm_token = ''
        new_cloudlet = MessageToDict(request, preserving_proto_field_name=True)
        # By default, protobuf converts uint64 type to string
        new_cloudlet['ram_size'] = int(new_cloudlet['ram_size'])
        new_cloudlet['rom_size'] = int(new_cloudlet['rom_size'])

        # Get the Docker Swarm Manager IP address and token to add a new worker
        with grpc.insecure_channel(self.scheduling_service_url) as channel:
            stub = cloudlets_service_pb2_grpc.SchedulingAPIStub(channel)
            response = stub.SwarmManager(cloudlets_service_pb2.Empty())
            if response.status.code != 200:
                return cloudlets_service_pb2.ResponseWithCloudlet(status=response.status)
            swarm_address = response.manager.address
            swarm_token = response.manager.join_worker_token

        try:
            # Adding a new fog device to the database
            self.mongo_client.server_info()
            object_id = self.cloudlets_collection.insert_one(new_cloudlet).inserted_id

            # Adding a fog device to an area by coordinates
            self.areas_collection.update_one({'_id': f'{int(request.latitude)}x{int(request.longitude)}'},
                                             {'$push': {'cloudlets': {
                                                 'id': str(object_id),
                                                 'latitude': request.latitude,
                                                 'longitude': request.longitude
                                             }}},
                                             upsert=True)
            # Adding an area to the list
            self.areas_collection.update_one({'_id': 'areas_id'},
                                             {'$addToSet': {'list': f'{int(request.latitude)}x{int(request.longitude)}'}},
                                             upsert=True)
        except pymongo.errors.DuplicateKeyError:
            # Handle an exception if the fog device with this IP address already exists
            existing_cloudlet = self.cloudlets_collection.find_one({'ip': request.ip})
            existing_cloudlet_proto = cloudlets_service_pb2.Cloudlet()
            ParseDict(existing_cloudlet, existing_cloudlet_proto, ignore_unknown_fields=True)
            existing_cloudlet_proto.id = str(existing_cloudlet['_id'])
            return cloudlets_service_pb2.ResponseWithCloudlet(status=cloudlets_service_pb2.Response(code=409, message='Cloudlet with this IP address already exists.'),
                                                              cloudlet=existing_cloudlet_proto,
                                                              swarm_manager_address=swarm_address,
                                                              swarm_worker_token=swarm_token)
        except Exception as error:
            return cloudlets_service_pb2.ResponseWithCloudlet(status=cloudlets_service_pb2.Response(code=500, message=f'An internal server error occurred. {error}'))

        request.id = str(object_id)
        return cloudlets_service_pb2.ResponseWithCloudlet(status=cloudlets_service_pb2.Response(code=201, message='Cloudlet has been added successfully.'),
                                                          cloudlet=request,
                                                          swarm_manager_address=swarm_address,
                                                          swarm_worker_token=swarm_token)

    # Returns a list of fog devices with the specified parameters
    def Find(self, request, context):
        params = {}
        for field in request.DESCRIPTOR.fields:
            value = getattr(request, field.name)
            if value:
                if field.name == 'id':
                    try:
                        params['_id'] = objectid.ObjectId(value)
                    except errors.InvalidId:
                        return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=422, message='Cloudlet id parameter is invalid.'))
                else:
                    params[field.name] = value

        search_result = {}
        try:
            self.mongo_client.server_info()
            search_result = self.cloudlets_collection.find(params)
        except Exception as error:
            return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=500, message=f'An internal server error occurred. {error}'))

        cloudlets = []
        for item in search_result:
            cloudlet = cloudlets_service_pb2.Cloudlet()
            for key, value in item.items():
                if key != '_id':
                    setattr(cloudlet, key, value)
                else:
                    setattr(cloudlet, 'id', str(value))
            cloudlets.append(cloudlet)

        if len(cloudlets) == 0:
            return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=404, message='No cloudlets were found with these parameters.'))
        else:
            return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=200, message='Search completed successfully.'),
                                                                   cloudlets=cloudlets)

    # Searches for nearby fog devices
    def FindNearest(self, request, context):
        if not (request.latitude and request.longitude):
            return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=422, message='Latitude and longitude parameters are required.'))

        searched_devices_count = 2
        search_area = Point(latitude=int(request.latitude), longitude=int(request.longitude))
        nearest_cloudlets = []
        try:
            self.mongo_client.server_info()
            cloudlets_count = self.cloudlets_collection.count()
            if cloudlets_count < searched_devices_count:
                return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=404, message='Fog nodes not found.'))

            areas_id = self.areas_collection.find_one({'_id': 'areas_id'}).get('list')
            dist_to_areas = {}
            for area_id in areas_id:
                area_latitude, area_longitude = area_id.split('x')
                area = Point(latitude=int(area_latitude), longitude=int(area_longitude))
                dist = calculate_dist(search_area.latitude, search_area.longitude, area.latitude, area.longitude)
                if dist_to_areas.get(dist):
                    dist_to_areas[dist].append(area)
                else:
                    dist_to_areas[dist] = [area]

            sorted_dist_to_areas = sorted(dist_to_areas)
            for dist_to_ares in sorted_dist_to_areas:
                dist_to_cloudlets = {}
                for area in dist_to_areas[dist_to_ares]:
                    cloudlets_in_area = self.areas_collection.find_one({'_id': str(area.latitude) + 'x' + str(area.longitude)}).get('cloudlets')
                    if cloudlets_in_area:
                        for cloudlet in cloudlets_in_area:
                            dist_to_cloudlet = calculate_dist(request.latitude, request.longitude, cloudlet.get('latitude'), cloudlet.get('longitude'))
                            if dist_to_cloudlets.get(dist_to_cloudlet):
                                dist_to_cloudlets[dist_to_cloudlet].append(cloudlet.get('id'))
                            else:
                                dist_to_cloudlets[dist_to_cloudlet] = [cloudlet.get('id')]

                sorted_dist_to_cloudlets = sorted(dist_to_cloudlets)
                for dist in sorted_dist_to_cloudlets:
                    left = searched_devices_count - len(nearest_cloudlets)
                    for id in dist_to_cloudlets.get(dist)[:left]:
                        nearest_cloudlets.append(cloudlets_service_pb2.Cloudlet(id=id))
                    if len(nearest_cloudlets) >= searched_devices_count:
                        break

                if len(nearest_cloudlets) >= searched_devices_count:
                    break

        except Exception as error:
            return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=500, message=f'An internal server error occurred. {error}'))

        return cloudlets_service_pb2.ResponseWithCloudletsList(status=cloudlets_service_pb2.Response(code=200, message='Nearest fog nodes were found.'),
                                                               cloudlets=nearest_cloudlets)
