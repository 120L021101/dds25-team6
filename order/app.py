import logging
import os
import atexit
import random
import uuid
from collections import defaultdict

import redis
import requests

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response
from redis.sentinel import Sentinel 
import time
import json

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']

app = Flask("order-service")

sentinel = Sentinel(
    [(os.environ["SENTINEL_HOST"], int(os.environ["SENTINEL_PORT"]))],
    socket_timeout=5,
    password=os.environ["REDIS_PASSWORD"],
)

# 始终从sentinel获得最新的master连接
def get_redis_connection():
    return sentinel.master_for("order-master", password=os.environ["REDIS_PASSWORD"], decode_responses=False)

# db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
#                               port=int(os.environ['REDIS_PORT']),
#                               password=os.environ['REDIS_PASSWORD'],
#                               db=int(os.environ['REDIS_DB']))

db = get_redis_connection()

def close_db_connection():
    db.close()


atexit.register(close_db_connection)


class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int


def get_order_from_db(order_id: str) -> OrderValue | None:
    try:
        # get serialized data
        entry: bytes = db.get(order_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    # deserialize data if it exists else return null
    entry: OrderValue | None = msgpack.decode(entry, type=OrderValue) if entry else None
    if entry is None:
        # if order does not exist in the database; abort
        abort(400, f"Order: {order_id} not found!")
    return entry


@app.post('/create/<user_id>')
def create_order(user_id: str):
    key = str(uuid.uuid4())
    value = msgpack.encode(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'order_id': key})


@app.post('/batch_init/<n>/<n_items>/<n_users>/<item_price>')
def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):

    n = int(n)
    n_items = int(n_items)
    n_users = int(n_users)
    item_price = int(item_price)

    def generate_entry() -> OrderValue:
        user_id = random.randint(0, n_users - 1)
        item1_id = random.randint(0, n_items - 1)
        item2_id = random.randint(0, n_items - 1)
        value = OrderValue(paid=False,
                           items=[(f"{item1_id}", 1), (f"{item2_id}", 1)],
                           user_id=f"{user_id}",
                           total_cost=2*item_price)
        return value

    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(generate_entry())
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get('/find/<order_id>')
def find_order(order_id: str):
    order_entry: OrderValue = get_order_from_db(order_id)
    return jsonify(
        {
            "order_id": order_id,
            "paid": order_entry.paid,
            "items": order_entry.items,
            "user_id": order_entry.user_id,
            "total_cost": order_entry.total_cost
        }
    )


def send_post_request(url: str, data: dict=None):
    try:
        response = requests.post(url, data=data)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)
    else:
        return response


def send_get_request(url: str):
    try:
        response = requests.get(url)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)
    else:
        return response


@app.post('/addItem/<order_id>/<item_id>/<quantity>')
def add_item(order_id: str, item_id: str, quantity: int):
    order_entry: OrderValue = get_order_from_db(order_id)
    item_reply = send_get_request(f"{GATEWAY_URL}/stock/find/{item_id}")
    if item_reply.status_code != 200:
        # Request failed because item does not exist
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    order_entry.items.append((item_id, int(quantity)))
    order_entry.total_cost += int(quantity) * item_json["price"]
    try:
        db.set(order_id, msgpack.encode(order_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} added to: {order_id} price updated to: {order_entry.total_cost}",
                    status=200)


def rollback_stock(removed_items: list[tuple[str, int]]):
    for item_id, quantity in removed_items:
        send_post_request(f"{GATEWAY_URL}/stock/add/{item_id}/{quantity}")

# This script is to log all the uncommited commit transactions(and delete from rollback), if the main server is down, backup can still continue to commit those
with open(file='2pc/commit_log.lua', mode='r') as f:
    log_commit = db.register_script(f.read())

# Opposite to log_commit
with open(file='2pc/commit_unlog.lua', mode='r') as f:
    unlog_commit = db.register_script(f.read())

# This script is to log all the unrollbacked commit transactions, if the main server is down, backup can still continue to rollback those
with open(file='2pc/rollback_log.lua', mode='r') as f:
    log_rollback = db.register_script(f.read())

# Opposite to log_rollback
with open(file='2pc/rollback_unlog.lua', mode='r') as f:
    unlog_rollback = db.register_script(f.read())

@app.post('/checkout/<order_id>')
def checkout_2pc(order_id: str):
    app.logger.info(f"Checking out {order_id}")
    order_entry: OrderValue = get_order_from_db(order_id)

    if order_entry.paid:
        return Response("Has been Checkout, can't checkout again", status=200)

    import uuid
    txn_id = str(uuid.uuid4())
    app.logger.info(f"Generated Transaction ID: {txn_id}")

    # log this transaction to rollback set before try to prepare
    log_item = json.dumps({
        "transaction_id" : txn_id,
        "order_id": order_id,
        "user_id" : order_entry.user_id,
        "items" : order_entry.items,
        "total_cost": order_entry.total_cost,
    })
    log_rollback(keys=[], args=[log_item])

    # prepare phase
    prepare_resp = [
        # payment prepare
        send_post_request(f"{GATEWAY_URL}/payment/checkout_prepare/{order_entry.user_id}/{txn_id}/{order_entry.total_cost}")
    ] + [
        # stock prepare
        # send_post_request(f"{GATEWAY_URL}/stock/checkout_prepare/{item_id}/{txn_id}/{amount}") 
        #     for (item_id, amount) in order_entry.items
    ]

    # nothing failed, add to commit set
    resp = Response("Checkout Failed, Please Retry Later", status=200)
    if all(resp.status_code == 200 for resp in prepare_resp):
        log_commit(keys=[], args=[log_item])
        order_entry.paid = True
        db.set(order_id, msgpack.encode(order_entry))
        resp = Response("Checkout admitted. Result may be updated later in mins", status=200)

    # finally first, try to rollback all the rollback set
    rollback_set = db.smembers("unrollback_txn_set")
    app.logger.info(f"Rollback {rollback_set}")

    for log_item_rllbck in [ json.loads(item.decode("utf-8")) for item in rollback_set ]:
        rollback_resp = [
            # payment rollback
            send_post_request(f"{GATEWAY_URL}/payment/checkout_rollback/{log_item_rllbck['user_id']}/{log_item_rllbck['transaction_id']}")
        ] + [
            # order rollback
        ]
        if all(resp.status_code == 200 for resp in rollback_resp):
            unlog_rollback(keys=[], args=[json.dumps(log_item_rllbck)])

    # finally then, try to commit all the commit set
    commit_set = db.smembers("uncommit_txn_set")
    app.logger.info(f"Rollback {commit_set}")

    for log_item_cmmt in [ json.loads(item.decode("utf-8")) for item in commit_set ]:
        commit_resp = [
            # payment commit
            send_post_request(f"{GATEWAY_URL}/payment/checkout_commit/" + 
                f"{log_item_cmmt['user_id']}/{log_item_cmmt['transaction_id']}/{log_item_cmmt['total_cost']}")
        ] + [
            # order commit
        ]
        app.logger.info(f"commit response: {commit_resp[0].text}")
        if all(resp.status_code == 200 for resp in commit_resp):
            unlog_commit(keys=[], args=[json.dumps(log_item_cmmt)])

    return resp


    # if True:
    #     send_post_request(f"{GATEWAY_URL}/payment/checkout_rollback/{order_entry.user_id}/{txn_id}")
    #     return Response("Checkout Failed, Please Retry Later", status=200)
    
    # app.logger.info(f"{prepare_resp}")

    # # commit phase
    # # first, log this to-be-committed transaction
    # log_commit(keys=[], args=[txn_id])

    # # try to commit it
    # COMMIT_RETRIES = 10
    # commit_resp_payment = None
    # for _ in range(COMMIT_RETRIES):
    #     commit_resp_payment = send_post_request(
    #         f"{GATEWAY_URL}/payment/checkout_commit/{order_entry.user_id}/{txn_id}/{order_entry.total_cost}"
    #     )
    #     app.logger.info(f"{commit_resp_payment}")
    #     if commit_resp_payment: # and order resp
    #         break
    
    # # update status of this order
    # order_entry.paid = True
    # for _ in range(COMMIT_RETRIES):
    #     try: db.set(order_id, msgpack.encode(order_entry))
    #     except: continue
        
    # # if successful, remove this transaction from the log set
    # unlog_commit(keys=[], args=[txn_id])

    # # TODO: should commit all the transactions in the uncommit set
    # # this is for failure recovery.

    # return Response("Checkout successful", status=200)

def checkout(order_id: str):
    app.logger.debug(f"Checking out {order_id}")
    order_entry: OrderValue = get_order_from_db(order_id)
    # get the quantity per item
    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in order_entry.items:
        items_quantities[item_id] += quantity
    # The removed items will contain the items that we already have successfully subtracted stock from
    # for rollback purposes.
    removed_items: list[tuple[str, int]] = []
    for item_id, quantity in items_quantities.items():
        stock_reply = send_post_request(f"{GATEWAY_URL}/stock/subtract/{item_id}/{quantity}")
        if stock_reply.status_code != 200:
            # If one item does not have enough stock we need to rollback
            rollback_stock(removed_items)
            abort(400, f'Out of stock on item_id: {item_id}')
        removed_items.append((item_id, quantity))
    user_reply = send_post_request(f"{GATEWAY_URL}/payment/pay/{order_entry.user_id}/{order_entry.total_cost}")
    if user_reply.status_code != 200:
        # If the user does not have enough credit we need to rollback all the item stock subtractions
        rollback_stock(removed_items)
        abort(400, "User out of credit")
    order_entry.paid = True
    try:
        db.set(order_id, msgpack.encode(order_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    app.logger.debug("Checkout successful")
    return Response("Checkout successful", status=200)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
