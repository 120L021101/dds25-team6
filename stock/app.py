import logging
import os
import atexit
import uuid

import redis

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response
from redis.sentinel import Sentinel 

DB_ERROR_STR = "DB error"

app = Flask("stock-service")

sentinel = Sentinel(
    [(os.environ["SENTINEL_HOST"], int(os.environ["SENTINEL_PORT"]))],
    socket_timeout=5,
    password=os.environ["REDIS_PASSWORD"],
)

# always get latest master connection from sentinel
def get_redis_connection():
    return sentinel.master_for("stock-master", password=os.environ["REDIS_PASSWORD"], decode_responses=False)

# db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
#                               port=int(os.environ['REDIS_PORT']),
#                               password=os.environ['REDIS_PASSWORD'],
#                               db=int(os.environ['REDIS_DB']))

db = get_redis_connection()

def close_db_connection():
    db.close()


atexit.register(close_db_connection)


class StockValue(Struct):
    stock: int
    price: int

@app.post('/item/create/<price>')
def create_item(price: int):
    key = str(uuid.uuid4())
    app.logger.debug(f"Item: {key} created")
    value = msgpack.encode(StockValue(stock=0, price=int(price)))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'item_id': key})


@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
def batch_init_users(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(StockValue(stock=starting_stock, price=item_price))
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})


@app.get('/find/<item_id>')
def find_item(item_id: str):
    item_entry: StockValue = get_item_from_db(item_id)
    return jsonify(
        {
            "stock": item_entry.stock,
            "price": item_entry.price
        }
    )


@app.post('/add/<item_id>/<amount>')
def add_stock(item_id: str, amount: int):
    item_entry: StockValue = get_item_from_db(item_id)
    # update stock, serialize and update database
    item_entry.stock += int(amount)
    try:
        db.set(item_id, msgpack.encode(item_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {item_entry.stock}", status=200)


@app.post('/subtract/<item_id>/<amount>')
def remove_stock(item_id: str, amount: int):
    item_entry: StockValue = get_item_from_db(item_id)
    # update stock, serialize and update database
    item_entry.stock -= int(amount)
    app.logger.debug(f"Item: {item_id} stock updated to: {item_entry.stock}")
    if item_entry.stock < 0:
        abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
    try:
        db.set(item_id, msgpack.encode(item_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {item_entry.stock}", status=200)


## Below is 2PC ##

# Temporary test func
@app.post('/item/luatest/<item_id>')
def lua_test(item_id: int):
    try:
        entry: bytes = db.get(item_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    
    return Response(status=200)

# Prepare 
with open(file='2pc/prepare.lua', mode='r') as f:
    checkout_prepare_script = db.register_script(f.read())

@app.post('/checkout_prepare/<item_id>/<transaction_id>/<amount>')
def checkout_prepare(item_id, transaction_id, amount: str):
    amount = int(amount)
    app.logger.info(f"[Stock]: PREPARE: {transaction_id}, {item_id}, {amount}")
    # lua: check + lock
    try:
        ret = checkout_prepare_script(keys=[item_id,], args=[transaction_id, amount])
        result = ret.decode("utf-8")
        app.logger.info(result)

        if "PREPARED" in result:
            app.logger.info(f"[Stock]: Prepared: {item_id}, {result}")
            return Response(result, status=200)
        elif "insufficient" in result:
            app.logger.error(f"[Stock<Error>]: Insufficient stock: {item_id}, {result}")
            return Response(result, status=400)  
        else:
            # should never enter this branch, can't prepare committed transaction etc.
            app.logger.error(f"[Stock<Error>]: Conflict: {item_id}, [prepare.lua]-{result}")
            return Response(result, status=409)  # Conflict
    except Exception as e:
        app.logger.error(f"[Stock] Redis Internal Error: in checkout prepare: {str(e)}")
        abort(404, "Internal Server Error")

def get_item_from_db(item_id):
    try:
        entry: bytes = db.get(item_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    entry: StockValue | None = msgpack.decode(entry, type=StockValue) if entry else None
    if entry is None:
        abort(409, f"Item: {item_id} not found!")
    return entry

# Commit
REDIS_RETRIES = 10

with open(file='2pc/rollback.lua', mode='r') as f:
    checkout_rollback_script = db.register_script(f.read())

@app.post('/checkout_rollback/<item_id>/<transaction_id>')
def checkout_rollback(item_id, transaction_id):
    app.logger.info(f"[Stock] Start ROLLBACK: {transaction_id}, {item_id}")
    
    try:
        ret = checkout_rollback_script(keys=[item_id], args=[transaction_id])
        result = ret.decode("utf-8")
        app.logger.info(f"[Stock] Rollback result: {result}")
        
        if "ROLLBACKED" in result:
            return Response(result, status=200)
        else:
            # No lock or not belonging to txn
            # therefore nothing to rollback, still reply success to client
            return Response(result, status=200)
    except Exception as e:
        app.logger.error(f"Error in checkout rollback: {str(e)}")
        return abort(404, "Internal Server Error")

with open(file='2pc/commit.lua', mode='r') as f:
    checkout_commit_script = db.register_script(f.read())


@app.post('/checkout_commit/<item_id>/<transaction_id>/<amount>')
def checkout_commit(item_id, transaction_id, amount: str):
    amount = int(amount)
    # 1. Get lock and check timeout
    # try: 
    #     lock_status = db.get(f"lock:{item_id}")
    #     if lock_status == None:
    #         # 1.5 Get lock failed
    #         pass
    # except redis.exceptions.RedisError:
    #     return abort(400, DB_ERROR_STR)
    # 2. Lua: get lock again, 
    # del txn, upd amount, del lock
    try:
        ret = checkout_commit_script(keys=[item_id,], args=[transaction_id, amount])
        result = ret.decode("utf-8")
        app.logger.info(f'[Stock Commit.lua] {result}')

        # 3. Return Response
        if "COMMITTED" in result:
            app.logger.info(f'[Stock Commit] Stock committed: {transaction_id}')
            return Response(result, status=200)
        # TODO: invalid branch, no such "Fatal Error" in return values
        elif "Fatal Error" in result:
            app.logger.info(f'[Stock Commit] Fatal error: {transaction_id}')
            return Response(result, status=404)  
    except Exception as e:
        app.logger.error(f"Error in checkout commit: {str(e)}")
        abort(404, "Internal Server Error")

    
## 2PC Ends ##

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
