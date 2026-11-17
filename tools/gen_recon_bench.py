#!/usr/bin/env python3
"""Generate large catalog snapshots for reconciliation benchmarks at runtime.

Nothing large is committed: both catalogs are streamed to disk in bounded
writes.  Each catalog holds ``--products`` products with up to
``--variants`` variants; the two snapshots differ in a deterministic,
hand-defined way so the benchmark can assert exact event counts without
calling any product diff logic.

Change recipe applied to new.xml relative to old.xml:
  * every 500th SKU exists only in the new catalog (product additions);
  * every 700th SKU (not already an addition) exists only in the old one
    (product removals);
  * every 137th surviving product changes its price field;
  * one variant is added (and one removed further down) on every 211th
    surviving product, and every 313th surviving product changes the stock
    field of its first variant;
  * every variant block is emitted in reversed order, exercising the
    unordered-collection rule on a massive scale.

Also writes mapping.json (consumed by reconcile) and facts.json (expected
event counts plus a hand-written probe table used by the benchmark).
"""

import argparse
import json
import os

NS = 'xmlns:c="urn:catalog"'
MAPPING = {
    "version": 1,
    "namespaces": {"c": "urn:catalog"},
    "product": {
        "select": [
            {"axis": "child", "test": {"kind": "name", "qname": "c:catalog"}},
            {"axis": "child", "test": {"kind": "name", "qname": "c:product"}},
        ],
        "sku": {"name": "product.sku", "path": [
            {"axis": "attribute",
             "test": {"kind": "name", "qname": "c:sku"}}]},
        "fields": [
            {"name": "product/name", "path": [
                {"axis": "child", "test": {"kind": "name", "qname": "c:name"}}]},
            {"name": "product/price", "path": [
                {"axis": "child", "test": {"kind": "name", "qname": "c:price"}}]},
        ],
        "variants": {
            "path": [
                {"axis": "child",
                 "test": {"kind": "name", "qname": "c:variant"}}],
            "code": {"name": "variant.code", "path": [
                {"axis": "attribute",
                 "test": {"kind": "name", "qname": "c:variantCode"}}]},
            "fields": [
                {"name": "variant/stock", "path": [
                    {"axis": "child",
                     "test": {"kind": "name", "qname": "c:stock"}}]}],
        },
    },
}


def sku_of(i):
    return "SKU%08d" % i


def write_catalog(path, n_products, n_variants, *, is_new):
    """Stream one catalog.  ``n_variants`` variants per product (the last
    product in old.xml uses one fewer to create removals deterministically)."""
    added = set(range(0, n_products, 500))
    removed = set(range(0, n_products, 700)) - added
    survivors = set(range(n_products)) - added - removed
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("<c:catalog %s>" % NS)
        buf = []

        def flush():
            if buf:
                fh.write("".join(buf))
                buf.clear()

        for i in range(n_products):
            if is_new and i in removed:
                continue
            if not is_new and i in added:
                continue
            sku = sku_of(i)
            # Integer cents only: float formatting would make 0.5+99.99 and
            # 0.99+99.99 print identically for some indices.  Prices are
            # kept below 1000 so the textual width is identical both sides.
            price_cents = (i % 500) * 100 + 50
            if is_new and i in survivors and i % 137 == 0:
                price_cents = (price_cents + 4999) % 100000
            price = "%d.%02d" % (price_cents // 100, price_cents % 100)
            buf.append('<c:product c:sku="%s"><c:name>name-%d</c:name>'
                       '<c:price>%s</c:price>' % (sku, i, price))

            codes = list(range(1, n_variants + 1))
            if i % 211 == 0 and i in survivors:
                if not is_new:
                    codes.append(999)  # removed in the new snapshot
                else:
                    codes.insert(0, 888)  # added in the new snapshot
            if is_new:
                codes.reverse()  # unordered-collection stress
            for code in codes:
                # Old values live in 0..499; new values in 500..999, so a
                # rewritten stock can never textually equal its old value.
                stock = str((i * 3 + code) % 500)
                if is_new and code == 1 and i in survivors and i % 313 == 0:
                    stock = str(500 + (i % 500))
                buf.append('<c:variant c:variantCode="%d"><c:stock>%s</c:stock>'
                           '</c:variant>' % (code, stock))
            buf.append("</c:product>")
            if len(buf) >= 2048:
                flush()
        flush()
        fh.write("</c:catalog>\n")


def expected_counts(n_products, n_variants):
    added = set(range(0, n_products, 500))
    removed = set(range(0, n_products, 700)) - added
    survivors = set(range(n_products)) - added - removed
    common = sorted(survivors)
    price_changes = [i for i in common if i % 137 == 0]
    touched = [i for i in common if i % 211 == 0]
    # Stock of pre-existing code 1 changes on every %313 survivor.
    stock_changes = [i for i in common if i % 313 == 0]
    # Added/removed products carry cascading variant entity events for
    # every variant they contain.
    return {
        "product_added": len(added),
        "product_removed": len(removed),
        "product_field_changed": len(price_changes),
        # Touched survivors: one 888 added / one 999 removed each, plus all
        # variants of added/removed products as cascading entity events.
        "variant_added": len(touched) + len(added) * n_variants,
        "variant_removed": len(touched) + len(removed) * n_variants,
        "variant_field_changed": len(stock_changes),
        "probes": {
            "first_added_sku": sku_of(min(added)),
            "first_removed_sku": sku_of(min(removed)),
            "first_price_change_sku": sku_of(price_changes[0]),
            "first_variant_touched_sku": sku_of(touched[0]),
            "first_stock_change_sku": sku_of(stock_changes[0]),
            "unrelated_stable_sku": sku_of(1),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--products", type=int, default=200000)
    ap.add_argument("--variants", type=int, default=2)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    old_path = os.path.join(args.outdir, "catalog-old.xml")
    new_path = os.path.join(args.outdir, "catalog-new.xml")
    write_catalog(old_path, args.products, args.variants, is_new=False)
    write_catalog(new_path, args.products, args.variants, is_new=True)
    with open(os.path.join(args.outdir, "mapping.json"), "w",
              encoding="utf-8") as fh:
        json.dump(MAPPING, fh)
    facts = expected_counts(args.products, args.variants)
    facts["products"] = args.products
    facts["variants_per_product"] = args.variants
    with open(os.path.join(args.outdir, "facts.json"), "w",
              encoding="utf-8") as fh:
        json.dump(facts, fh, indent=1, sort_keys=True)
    print("generated %s and %s (%d products, %d variants each)"
          % (old_path, new_path, args.products, args.variants))


if __name__ == "__main__":
    main()
