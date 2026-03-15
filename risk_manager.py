consecutive_losses = 0
total_losses = 0

def check_risk():
    global consecutive_losses
    global total_losses

    if consecutive_losses >= 3:
        return "PAUSE"

    if total_losses >= 5:
        return "STOP"

    return "TRADE"