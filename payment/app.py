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

# always get latest master connection from sentinel
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
    app.logger.info(f"[Payment] PREPARE: {transaction_id}, {user_id}")

    user_entry = get_user_from_db(user_id=user_id)
    if user_entry.credit < int(amount):
        """ cannot prepare """
        app.logger.error(f"Insufficient Money")
        return Response(f"Insufficient Money", status=500)

    ret : bytes = checkout_prepare_script(keys=[user_id,], args=[transaction_id,])
    if ret.decode('utf-8') == 'Failed, Already Locked':
        return Response(f"[Payment] Transaction {transaction_id} is already in progress", status=409)
    
    app.logger.info(f"[checkout_prepare.lua] Success: {ret.decode('utf-8')}")
    app.logger.info(f"[Payment] PREPARED: {transaction_id}, {user_id}")
    app.logger.info(f"Lock status after prepare: {db.get('lock:'+user_id)}")
    return Response(f"{ret.decode("utf-8")}", status=200)

# Commit
REDIS_RETRIES = 10

'''
this function should hold idempotence,
even the response message should be the same
'''
@app.post('/checkout_commit/<user_id>/<transaction_id>/<amount>')
def checkout_commit(user_id, transaction_id, amount: str):
    # class impedance, have to do commit here
    # app.logger.info(f"[payment] COMMIT: {transaction_id}, {user_id}")

    # get user object
    user_entry = None
    for _ in range(REDIS_RETRIES):
        user_entry = get_user_from_db(user_id=user_id)
        if user_entry is None:
            time.sleep(1.0)

    app.logger.info(f"[payment] Now COMMIT: {transaction_id}, {user_id}")
    # lookup the current transaction status
    status: bytes | None = db.get("txn:" + transaction_id)
    if status is not None:
        status = status.decode("utf-8")
        app.logger.info(f"current state: {status}")
        if status.startswith("COMMITTED"):
            return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)
        elif not status.startswith("PREPARED"):
            return Response(f"{transaction_id} has been either rollbacked or never existing", status=409)
    else:
        # Scenario: Transaction not found? -- should never happen
        app.logger.info(f"[payment_commit<ERROR>] Transaction {transaction_id} not found")
        return Response(f"{transaction_id} not found", status=404)
    
    # construct lock_key
    lock_key = "lock:" + user_id
    trx_id_lock = None
    for _ in range(REDIS_RETRIES):
        try: 
            trx_id_lock = db.get(lock_key).decode('utf-8')
            break
        except: time.sleep(0.1)
    
    # current lock is not owned by this transaction
    # actually, should never happen, So only for debugging, currently use assert
    assert trx_id_lock == transaction_id, f"Lock Owner Error! Owner is {trx_id_lock}, This is {transaction_id}"

    # subtract amount
    user_entry.credit -= int(amount)

    # store the changes
    try:
        db.set(user_id, msgpack.encode(user_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    
    # Release the lock, update the transaction status to commited
    db.set("txn:" + transaction_id, f"COMMITTED")
    db.delete(lock_key)

    return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)


with open(file='2pc/rollback.lua', mode='r') as f:
    checkout_rollback_script = db.register_script(f.read())

@app.post('/checkout_rollback/<user_id>/<transaction_id>')
def checkout_rollback(user_id, transaction_id):
    app.logger.info(f"[Payment] Start Rollback {transaction_id}, {user_id}")
    # lookup the current transaction status
    status: bytes | None = db.get("txn:"+transaction_id)
    
    if status is not None:
        status = status.decode("utf-8")
        if status.startswith("ROLLBACKED"):
            return Response(f"[Payment] Rollback {transaction_id} Successfully", status=200)
        elif status.startswith("COMMITTED"):
            return Response(f"[Payment] Cannot Rollback a committed transaction", status=409)

    ret = checkout_rollback_script(keys=[user_id], args=[transaction_id])
    if ret.decode('utf-8') == 'ROLLBACKED':
        return Response(f"[Payment Success] Rollback {transaction_id} Successfully", status=200)
    else:
        return Response(f"[Payment Failed] Rollback {transaction_id} Failed", status=409)
    

## 2PC Ends ##

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
