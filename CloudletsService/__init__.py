import grpc
from concurrent import futures
from CloudletsService import cloudlets_service_pb2_grpc
from CloudletsService.methods import CloudletsAPI

server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
cloudlets_service_pb2_grpc.add_CloudletsAPIServicer_to_server(CloudletsAPI(), server)
server.add_insecure_port('[::]:50050')
