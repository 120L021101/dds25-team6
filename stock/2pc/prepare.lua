-- prepare.lua
-- stock service

-- item key
local item_key = KEYS[1]
local txn_id = ARGV[1]
local amount = ARGV[2]

local lock_key = item_key .. ":lock"
local txn_key = txn_id .. ":txn"
-- trx key, should be same in a trx

local stock_value_structure = redis.call("GET", item_key)

-- Deconstruction with `msgpack`, get amount and cost
local stock_value = cmsgpack.unpack(stock_value_structure)
local current_amount = stock_value.stock

if tonumber(current_amount) >= tonumber(amount) then
    if redis.call("SETNX", lock_key, txn_key) == 1 then
        redis.call("SET", txn_key, "PREPARED")
        return "PREPARED"
    else
        return "Failed, already locked"
    end
else
    return "Failed, Stock insufficient!"
end