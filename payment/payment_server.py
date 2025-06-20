import os, uuid, time, atexit, logging
import grpc
from concurrent import futures
import redis
from msgspec import msgpack, Struct

import payment_pb2, payment_pb2_grpc
from redis.sentinel import Sentinel

# 初始化 logger
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("payment_service")

DB_ERROR_STR = "DB error"

# Redis setup
sentinel = Sentinel([(os.environ["SENTINEL_HOST"], int(os.environ["SENTINEL_PORT"]))],
                    socket_timeout=5, password=os.environ["REDIS_PASSWORD"])
def get_redis_connection():
    return sentinel.master_for("payment-master",
                               password=os.environ["REDIS_PASSWORD"],
                               decode_responses=False)
db = get_redis_connection()
atexit.register(lambda: db.close())

class UserValue(Struct):
    credit: int

def get_user(user_id, context):
    try:
        logger.debug(f"Fetching user {user_id} from Redis")
        entry = db.get(user_id)
    except redis.exceptions.RedisError as e:
        logger.error(f"Redis error getting user {user_id}: {e}")
        context.abort(grpc.StatusCode.INTERNAL, DB_ERROR_STR)
    if not entry:
        logger.warning(f"User {user_id} not found")
        context.abort(grpc.StatusCode.NOT_FOUND, f"User {user_id} not found")
    return msgpack.decode(entry, type=UserValue)

class PaymentServicer(payment_pb2_grpc.PaymentServiceServicer):

    def CheckoutPrepare(self, request, context):
        logger.info(f"Prepare checkout tx {request.transaction_id} for user {request.user_id} amount {request.amount}")
        user = get_user(request.user_id, context)
        if user.credit < request.amount:
            logger.warning(f"User {request.user_id} insufficient funds for checkout prepare")
            # context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Insufficient funds")
            return payment_pb2.Status(msg="Insufficient funds", code=500)
        ret = db.register_script(open('2pc/prepare.lua').read())(
            keys=[request.user_id], args=[request.transaction_id])
        msg = ret.decode()
        if "Locked" in msg:
            logger.warning(f"Transaction {request.transaction_id} already locked")
            # context.abort(grpc.StatusCode.ALREADY_EXISTS, msg)
            return payment_pb2.Status(msg=msg, code=500)
        logger.info(f"Checkout prepare success: {msg}")
        return payment_pb2.Status(msg=msg, code=200)

    def CheckoutCommit(self, request, context):
        logger.info(f"Commit checkout tx {request.transaction_id} for user {request.user_id} amount {request.amount}")
        user = get_user(request.user_id, context)
        status = db.get("txn:" + request.transaction_id)
        if status is None or not status.decode().startswith("PREPARED"):
            logger.error(f"Invalid txn status for commit: {status}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "No prepared transaction")
        user.credit -= request.amount
        try:
            db.set(request.user_id, msgpack.encode(user))
            db.set("txn:"+request.transaction_id, "COMMITTED")
            db.delete("lock:"+request.user_id)
        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error during checkout commit: {e}")
            context.abort(grpc.StatusCode.INTERNAL, DB_ERROR_STR)
        logger.info(f"Checkout commit success for tx {request.transaction_id}")
        return payment_pb2.UserDataResponse(user_id=request.user_id, credit=user.credit)

    def CheckoutRollback(self, request, context):
        logger.info(f"Rollback checkout tx {request.transaction_id} for user {request.user_id}")
        status = db.get("txn:" + request.transaction_id)
        if not status or status.decode().startswith("ROLLBACK"):
            logger.info(f"Transaction {request.transaction_id} already rollbacked")
            return payment_pb2.Status(msg="Already rollbacked", code=200)
        if status.decode().startswith("COMMITTED"):
            logger.warning(f"Cannot rollback committed tx {request.transaction_id}")
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Cannot rollback committed")
        ret = db.register_script(open('2pc/rollback.lua').read())(
            keys=[request.user_id], args=[request.transaction_id])
        logger.info(f"Rollback script result: {ret.decode()}")
        return payment_pb2.Status(msg=ret.decode(), code=200)

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    payment_pb2_grpc.add_PaymentServiceServicer_to_server(PaymentServicer(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    logger.info("gRPC server started on port 50051")
    server.wait_for_termination()

if __name__ == "__main__":
    serve()
