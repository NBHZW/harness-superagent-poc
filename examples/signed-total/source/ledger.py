from decimal import Decimal


def total(amounts):
    return sum((Decimal(x) for x in amounts if Decimal(x) > 0), Decimal(0))
