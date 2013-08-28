

class BaseDelivery(object):

    def check_availability(self, customer, order):
        raise NotImplementedError("This method is not implemented")
