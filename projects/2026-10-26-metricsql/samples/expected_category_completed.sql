WITH m0 AS (
  SELECT "products"."category" AS "products__category", SUM("order_items"."quantity") AS "units", AVG("order_items"."unit_price") AS "avg_unit_price"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders" ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "products" AS "products" ON "order_items"."product_id" = "products"."product_id"
  LEFT JOIN "customers" AS "customers" ON "orders"."customer_id" = "customers"."customer_id"
  WHERE "customers"."region" IN (?, ?)
  GROUP BY 1
),
m1 AS (
  SELECT "products"."category" AS "products__category", SUM("order_items".quantity * "order_items".unit_price) AS "completed_revenue"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders" ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "products" AS "products" ON "order_items"."product_id" = "products"."product_id"
  LEFT JOIN "customers" AS "customers" ON "orders"."customer_id" = "customers"."customer_id"
  WHERE "customers"."region" IN (?, ?) AND "orders"."status" = ?
  GROUP BY 1
),
spine AS (
  SELECT "products__category" FROM m0
  UNION
  SELECT "products__category" FROM m1
)
SELECT spine."products__category",
       COALESCE(m1."completed_revenue", 0) AS "completed_revenue",
       COALESCE(m0."units", 0) AS "units",
       m0."avg_unit_price" AS "avg_unit_price"
FROM spine
LEFT JOIN m0 ON m0."products__category" IS spine."products__category"
LEFT JOIN m1 ON m1."products__category" IS spine."products__category"
ORDER BY 2 DESC, 1 ASC
LIMIT ?;
-- params: ["north", "south", "north", "south", "completed", 3]
