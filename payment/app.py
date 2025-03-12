import logging
import os
import atexit
import uuid

import redis

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response
from redis.sentinel import Sentinel
import time

DB_ERROR_STR = "DB error"


app = Flask("payment-service")

sentinel = Sentinel(
    [(os.environ["SENTINEL_HOST"], int(os.environ["SENTINEL_PORT"]))],
    socket_timeout=5,
    password=os.environ["REDIS_PASSWORD"],
)

# 始终从sentinel获得最新的master连接
def get_redis_connection():
    return sentinel.master_for("payment-master", password=os.environ["REDIS_PASSWORD"], decode_responses=False)

# db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
#                               port=int(os.environ['REDIS_PORT']),
#                               password=os.environ['REDIS_PASSWORD'],
#                               db=int(os.environ['REDIS_DB']))

db = get_redis_connection()


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


class UserValue(Struct):
    credit: int


def get_user_from_db(user_id: str) -> UserValue | None:
    try:
        # get serialized data
        entry: bytes = db.get(user_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    # deserialize data if it exists else return null
    entry: UserValue | None = msgpack.decode(entry, type=UserValue) if entry else None
    if entry is None:
        # if user does not exist in the database; abort
        abort(400, f"User: {user_id} not found!")
    return entry


@app.post('/create_user')
def create_user():
    key = str(uuid.uuid4())
    value = msgpack.encode(UserValue(credit=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'user_id': key})


@app.post('/batch_init/<n>/<starting_money>')
def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(UserValue(credit=starting_money))
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
def find_user(user_id: str):
    user_entry: UserValue = get_user_from_db(user_id)
    return jsonify(
        {
            "user_id": user_id,
            "credit": user_entry.credit
        }
    )


@app.post('/add_funds/<user_id>/<amount>')
def add_credit(user_id: str, amount: int):
    user_entry: UserValue = get_user_from_db(user_id)
    # update credit, serialize and update database
    user_entry.credit += int(amount)
    try:
        db.set(user_id, msgpack.encode(user_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)


@app.post('/pay/<user_id>/<amount>')
def remove_credit(user_id: str, amount: int):
    app.logger.debug(f"Removing {amount} credit from user: {user_id}")
    user_entry: UserValue = get_user_from_db(user_id)
    # update credit, serialize and update database
    user_entry.credit -= int(amount)
    if user_entry.credit < 0:
        abort(400, f"User: {user_id} credit cannot get reduced below zero!")
    try:
        db.set(user_id, msgpack.encode(user_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)




## Below is 2PC ##

# Prepare 
with open(file='2pc/prepare.lua', mode='r') as f:
    checkout_prepare_script = db.register_script(f.read())

@app.post('/checkout_prepare/<user_id>/<transaction_id>/<amount>')
def checkout_prepare(user_id, transaction_id, amount: str):
    app.logger.info(f"PREPARE: {transaction_id}, {user_id}")
    user_entry = get_user_from_db(user_id=user_id)
    if user_entry.credit < int(amount):
        """ cannot prepare """
        return Response(f"Insufficient Money", status=500)

    ret = checkout_prepare_script(keys=[user_id,], args=[transaction_id,])
    app.logger.info(ret)
    return Response(f"{ret.decode("utf-8")}", status=200)

# Commit
REDIS_RETRIES = 10

@app.post('/checkout_commit/<user_id>/<transaction_id>/<amount>')
def checkout_commit(user_id, transaction_id, amount: str):
    # class impedance, have to do commit here
    app.logger.info(f"COMMIT: {transaction_id}, {user_id}")

    # construct lock_key
    lock_key = user_id + ":lock"
    trx_id_lock = None
    for _ in range(REDIS_RETRIES):
        try: trx_id_lock = db.get(lock_key).decode('utf-8')
        except: time.sleep(1.0)
    
    # current lock is not owned by this transaction
    # actually, should never happen, So only for debugging, currently use assert
    assert trx_id_lock == transaction_id, f"Lock Owner Error! Owner is {trx_id_lock}, This is {transaction_id}"

    # get user object
    user_entry = None
    for _ in range(REDIS_RETRIES):
        user_entry = get_user_from_db(user_id=user_id)
        if user_entry is not None:
            break

    # subtract amount
    user_entry.credit -= int(amount)

    # store the changes
    try:
        db.set(user_id, msgpack.encode(user_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    
    # Release the lock
    db.delete(lock_key)

    return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)


with open(file='2pc/rollback.lua', mode='r') as f:
    checkout_rollback_script = db.register_script(f.read())

@app.post('/checkout_rollback/<user_id>/<transaction_id>')
def checkout_rollback(user_id, transaction_id):

    ret = checkout_rollback_script(keys=[user_id], args=[transaction_id])
    return jsonify({"status" : ret.decode("utf-8")})

## 2PC Ends ##

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
