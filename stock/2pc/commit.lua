-- commit.lua
-- stock service

-- item key
local item_key = KEYS[1]
local txn_id = ARGV[1]
local amount = ARGV[2]

-- local lock_key = "lock:" .. item_key
local txn_key = "txn:" .. txn_id
local freeze_key = "freeze:" .. item_key
-- trx key, should be same in a trx

if redis.call("EXISTS", freeze_key) == 0 then
    return "Failed, Freeze data not found"
end

-- 检查item_key是否存在
if redis.call("EXISTS", item_key) == 0 then
    return "Failed, Item not found"
end

local txn_status = redis.call("GET", txn_key)

-- ensure idempotence
if txn_status == "COMMITTED" then
    return "COMMITTED (before)"
end

if txn_status ~= "PREPARED" and txn_status ~= "COMMITTED" then
    return "Failed, Transaction not in PREPARED state: " .. (txn_status or "nil")
end

local freeze_value_structure = redis.call("GET", freeze_key)
local freeze_value = cmsgpack.unpack(freeze_value_structure)
-- local reduce_amount = freeze_value.stock

-- upd txn, upd amount, del lock

local stock_value_structure = redis.call("GET", item_key)
local stock_value = cmsgpack.unpack(stock_value_structure)
stock_value.stock = stock_value.stock - tonumber(amount)

redis.call("SET", txn_key, "COMMITTED")
redis.call("SET", item_key, cmsgpack.pack(stock_value))
-- redis.call("DEL", freeze_key)
return "COMMITTED"
