# -*- encoding: utf-8 -*-
from __future__ import absolute_import
import trafaret as t
from trafaret import extras as te

from flamaster.core import http, _security
from flamaster.core.decorators import login_required, api_resource
from flamaster.core.resources import Resource, ModelResource
from flamaster.core.utils import (jsonify_status_code, null_fields_filter,
                                                                    send_email)
from flamaster.product.models import Cart

from flask import abort, request, session, g, current_app, json
from flask.ext.babel import lazy_gettext as _, get_locale
from flask.ext.principal import AnonymousIdentity, identity_changed
from flask.ext.security import (logout_user, login_user, current_user,
                                roles_required)
from flask.ext.security.utils import verify_password, encrypt_password
from flask.ext.security.confirmable import (confirm_email_token_status,
                                            confirm_user, requires_confirmation)
from flask.ext.security.registerable import register_user
from flask.ext.security.recoverable import (generate_reset_password_token,
                                            reset_password_token_status)
from flask.ext.security.signals import reset_password_instructions_sent

from sqlalchemy import or_

from . import bp
from .models import User, Role, BankAccount, Address, Customer

__all__ = ['SessionResource', 'ProfileResource', 'RoleResource']


@api_resource(bp, 'sessions', {'id': None})
class SessionResource(Resource):
    validation = t.Dict({
        'email': t.Email,
        'password': t.Or(t.String(allow_blank=True), t.Null),
        'confirm_password': t.Or(t.String(allow_blank=True), t.Null),
        'token': t.Or(t.String(allow_blank=True), t.Null),
        'reset': t.Bool
    }).make_optional('password', 'confirm_password', 'token',
                    'reset', 'email').ignore_extra('*')

    def get(self, id=None):
        return jsonify_status_code(self._get_response())

    def post(self):
        try:
            data = self.clean(request.json)

            if not User.is_unique(data['email']):
                raise t.DataError({'email': _("This email is already taken")})

            register_user(email=data['email'],
                          password=data.get('password', '*'))

            response, status = self._get_response(), http.CREATED

        except t.DataError as e:
            response, status = e.as_dict(), http.BAD_REQUEST
        return jsonify_status_code(response, status)

    def __change_password(self, password, confirm_password, token):
        expired, invalid, user = reset_password_token_status(token)
        if invalid or expired:
            return jsonify_status_code({'token': _("Wrong token")},
                                                        http.BAD_REQUEST)

        if password != confirm_password:
            return jsonify_status_code(
                        {'confirm_password': _("Passwords do not match")},
                        http.BAD_REQUEST
                    )

        user.update(password=password)
        login_user(user)
        return jsonify_status_code({'status': 'success'}, http.ACCEPTED)

    def __reset_password(self, email):
        user = User.query.filter_by(email=email).all()

        if user:
            token = generate_reset_password_token(user[0])
            url = 'reset_password/%s' % token
            reset_link = request.url_root + url

            subject = "Reset password"
            recipient = user[0].email
            template = "reset_password"
            params = {
                'user': user[0].first_name or user[0].email,
                'reset_link': reset_link
            }
            send_email(subject, recipient, template, **params)

            reset_password_instructions_sent.send(
                                    current_app._get_current_object(),
                                    user=user[0], token=token
                                )
            response = {'status': 'success'}
            return jsonify_status_code(response, http.ACCEPTED)
        return jsonify_status_code({'email': _("This email is not found")},
                                                                http.NOT_FOUND)

    def put(self, id):
        status = http.ACCEPTED

        try:
            cleaned_data = self.clean(request.json)
        except t.DataError as e:
            return jsonify_status_code(e.as_dict(), http.BAD_REQUEST)

        password = cleaned_data.get('password')
        confirm_password = cleaned_data.get('confirm_password')
        token = cleaned_data.get('token')
        if None not in (confirm_password, token):
            return self.__change_password(password, confirm_password, token)

        if cleaned_data.pop('reset', False):
            return self.__reset_password(cleaned_data['email'])

        try:
            self._authenticate(cleaned_data)
        except t.DataError as e:
            response, status = e.as_dict(), http.BAD_REQUEST
        else:
            response = self._get_response()

        return jsonify_status_code(response, status)

    def delete(self, id):
        for key in ('identity.name', 'identity.auth_type', 'customer_id'):
            session.pop(key, None)

        identity_changed.send(current_app._get_current_object(),
                              identity=AnonymousIdentity())
        logout_user()
        return jsonify_status_code(self._get_response(), http.NO_CONTENT)

    def clean(self, data_dict):
        return self.validation.check(data_dict)

    def _authenticate(self, data_dict):
        user = _security.datastore.find_user(email=data_dict['email'])

        if user is None:
            raise t.DataError({
                'email': _("Can't find anyone with this credentials")
            })

        if requires_confirmation(user):
            raise t.DataError({
                'email': _("You must confirm the email")
            })

        if not user.active:
            raise t.DataError({
                'email': _("You are blocked by the administrator")
            })

        if verify_password(data_dict.get('password'), user.password):
            if requires_confirmation(user):
                raise t.DataError({
                    'email': _("You must confirm the email")
                })

            if not user.active:
                raise t.DataError({
                    'email': _("You are blocked by the administrator")
                })

            login_user(user)

            # Get cart items from anonymous customer
            customer_id = session.get('customer_id')

            if customer_id is not None:
                customer = Customer.query.get(customer_id)

                if customer is not None and customer.user_id is None:
                    Cart.for_customer(customer).update({
                        'customer_id': user.customer.id})
                    customer.delete()

            session['customer_id'] = user.customer.id

        else:
            raise t.DataError({
                'password': _("Wrong password")
            })

        return data_dict

    def _get_response(self, **kwargs):
        response = {
            'id': session['id'],
            'is_anonymous': current_user.is_anonymous(),
            'uid': session.get('user_id'),
            'system_language': session.get('system_language') or get_locale().language
        }

        if not current_user.is_anonymous() and current_user.is_superuser():
            response['data_language'] = session.get('data_language') or get_locale().language
        response.update(kwargs)
        return response


# @api_resource(bp, 'profiles', {'id': int})
class ProfileResource(ModelResource):

    validation = t.Dict({'first_name': t.String,
                         'last_name': t.String,
                         'phone': t.String}).ignore_extra('*')
    model = User

    # method_decorators = {
    #     'get': [login_required, check_permission('profile_owner')]}

    def post(self):
        raise NotImplemented('Method is not implemented')

    def set_put_validation_dict(self, id):
        self.validation = t.Dict({
            'first_name': t.String,
            'last_name': t.String,
            'phone': t.String(allow_blank=True),
            'fax': t.String(allow_blank=True),
            'company': t.String(allow_blank=True),
            'role_id': t.Int,
            te.KeysSubset('password', 'confirmation'): self._cmp_pwds,
            }).append(self._change_role(id)).make_optional('role_id'). \
            ignore_extra('*')

    def put(self, id):
        """ we should check for password matching if
            user is trying to update it
        """
        self.set_put_validation_dict(id)

        status = http.ACCEPTED
        data = request.json or abort(http.BAD_REQUEST)

        NULL_FIELDS = ['phone', 'fax', 'company']
        data = null_fields_filter(NULL_FIELDS, data)

        try:
            data = self.clean(data)
            instance = self.get_object(id).update(with_reload=True, **data)
            response = self.serialize(instance)
        except t.DataError as e:
            status, response = http.BAD_REQUEST, e.as_dict()

        return jsonify_status_code(response, status)


    def _cmp_pwds(self, value):
        """ Password changing for user
        """
        if 'password' not in value and 'confirmation' not in value:
            return value

        elif len(value['password']) < 6:
            return {'password': t.DataError(_("Passwords should be more "
                                              "than 6 symbols length"))}
        elif value['password'] != value['confirmation']:
            return {'confirmation': t.DataError(_("Passwords doesn't match"))}

        return {'password': encrypt_password(value['password'])}

    def _change_role(self, id):
        """ helper method for changing user role if specified and current_user
            has administrator rights
        """
        def wrapper(value):
            user = self.get_object(id)
            if 'role_id' in value:
                role = Role.query.get_or_404(value['role_id'])
                if user.has_role(role):
                    return value
                elif current_user.is_superuser():
                    user.roles.append(role)
                    return value
                else:
                    abort(403, _('Role change not allowed'))
            return value
        return wrapper

    def get_object(self, id):
        """ overriding base get_object flow
        """
        if request.json and 'token' in request.json:
            token = request.json['token']
            expired, invalid, instance = confirm_email_token_status(token)
            confirm_user(instance)
            instance.save()
            login_user(instance, True)
        elif current_user.is_superuser():
            instance = User.query.get_or_404(id)
        else:
            instance = g.user

        instance is None and abort(http.NOT_FOUND)
        return instance

    def get_objects(self, *args, **kwargs):
        arguments = request.args.to_dict()
        allowed_args = ('first_name', 'last_name', 'email')
        filters = list(
                (getattr(self.model, arg).like(u'%{}%'.format(arguments[arg]))
                    for arg in arguments.iterkeys() if arg in allowed_args))
        self.model is None and abort(http.INTERNAL_ERR)
        return self.model.query.filter(or_(*filters))

    def __add_address_prefix(self, address, prefix):
        if address is None:
            return {}
        else:
            address_dict = dict(('{}_{}'.format(prefix, key), value)
                                  for key, value in address.as_dict().items())
        return address_dict

    def serialize(self, instance, include=None):
        exclude = ['password']
        include = ["first_name", "last_name", "created_at", "phone",
                   "current_login_at", "active", "billing_address",
                   "delivery_address", "logged_at", 'is_superuser', "birth_date",
                   "fax", "company", "gender", "id"]
        # include = ['is_superuser']

        if g.user.is_anonymous() or instance.is_anonymous():
            return instance.as_dict(include, exclude)

        if g.user.id != instance.id and g.user.is_superuser() is False:
            exclude.append('email')
        else:
            include.append('email')

        response = instance.as_dict(include, exclude)

        if instance.customer:
            response['shop_id'] = instance.customer.shop_id

        response.update(self.__add_address_prefix(instance.billing_address,
                                                                'billing'))
        response.update(self.__add_address_prefix(instance.delivery_address,
                                                                'delivery'))

        return response


@api_resource(bp, 'addresses', {'id': int})
class AddressResource(ModelResource):
    model = Address
    validation = t.Dict({
        'country_id': t.Int,
        'apartment': t.Or(t.String(allow_blank=True), t.Null),
        'city': t.String,
        'street': t.String,
        'type': t.String(regex="(billing|delivery)"),
        'zip_code': t.String,
        'first_name': t.String(allow_blank=True),
        'last_name': t.String(allow_blank=True),
        'company': t.String(allow_blank=True),
        'gender': t.String,
        'phone': t.String
    }).make_optional('apartment', 'first_name', 'last_name', 'company',
                     'type').ignore_extra('*')

    def post(self):
        status = http.CREATED
        # Hack for IE XDomainRequest support:

        try:
            data = self._request_data

            address_type = data.pop('type', None)
            address = self.model.create(**data)
            customer = self._customer()

            customer.set_address(address, address_type)
            customer.save()

            response = self.serialize(address)
        except t.DataError as e:
            status, response = http.BAD_REQUEST, e.as_dict()

        return jsonify_status_code(response, status)

    def get_objects(self, **kwargs):
        """ Method for extraction object list query
        """
        if current_user.is_anonymous() or not current_user.is_superuser():
            customer = self._customer()
            kwargs['customer_id'] = customer.id

        return super(AddressResource, self).get_objects(**kwargs)

    def _customer(self):
        if current_user.is_anonymous():
            customer_id = session.get('customer_id')

            if customer_id is not None:
                customer = Customer.query.get(customer_id)
            else:
                abort(http.BAD_REQUEST)
        else:
            customer = current_user.customer

        return customer

    @property
    def _request_data(self):
        try:
            data = request.json or json.loads(request.data)
            return self.clean(data)
        except t.DataError as err:
            raise err
        except:
            abort(http.BAD_REQUEST)


@api_resource(bp, 'roles', {'id': int})
class RoleResource(ModelResource):

    model = Role
    validation = t.Dict({'name': t.String}).ignore_extra('*')
    decorators = [login_required]
    method_decorators = {'post': roles_required('admin'),
                         'put': roles_required('admin')}

    def delete(self, id):
        """ We forbid roles removal """
        abort(http.METHOD_NOT_ALLOWED)


@api_resource(bp, 'bank_accounts', {'id': int})
class BankAccountResource(ModelResource):
    model = BankAccount
    validation = t.Dict({
            'bank_name': t.String,
            'iban': t.String,
            'swift': t.String
    }).ignore_extra('*')
    decorators = [login_required]

    def post(self):
        status = http.CREATED
        data = request.json or abort(http.BAD_REQUEST)

        try:
            data = self.clean(data)
            data['user_id'] = current_user.id
            response = self.serialize(self.model.create(**data))
        except t.DataError as err:
            response, status = err.as_dict(), http.BAD_REQUEST

        return jsonify_status_code(response, status)

    def get_object(self, id):
        instance = super(BankAccountResource, self).get_object(id)
        if instance.check_owner(current_user) or current_user.is_superuser():
            return instance
        return abort(http.UNAUTHORIZED)

    def get_objects(self, **kwargs):
        """ Method for extraction object list query
        """
        self.model is None and abort(http.BAD_REQUEST)
        if 'user_id' in request.args:
            kwargs['user_id'] = request.args['user_id']

        if not current_user.is_superuser():
            kwargs['user_id'] = current_user.id

        return self.model.query.filter_by(**kwargs)


@api_resource(bp, 'customers', {'id': int})
class CustomerResource(ModelResource):
    model = Customer
    method_decorators = {'delete': roles_required('admin')}
    validation = t.Dict({
        'first_name': t.String,
        'last_name': t.String,
        'email': t.Email,
        'phone': t.String(allow_blank=True),
        'notes': t.Or(t.String(allow_blank=True), t.Null),
        'fax': t.String(allow_blank=True),
        'company': t.String(allow_blank=True),
        'gender': t.String,
        'birth_date': t.DateTime,
        'direct_debit': t.Bool,
        'swift': t.String,
        'account_number': t.String,
        'blz': t.String
    }).make_optional('phone', 'notes', 'fax', 'company', 'birth_date',
                     'direct_debit', 'swift', 'account_number', 'blz')\
        .ignore_extra('*')

    # IE CORS Hack
    def post(self):
        status = http.CREATED

        try:
            data = self._request_data
            customer = self._customer()
            customer.update(**data)
            response = self.serialize(customer)
        except t.DataError as err:
            status, response = http.BAD_REQUEST, err.as_dict()

        return jsonify_status_code(response, status)

    def put(self, id):
        status = http.ACCEPTED
        try:
            data = self._request_data
            instance = self.get_object(id).update(with_reload=True, **data)
            response = self.serialize(instance)
        except t.DataError as e:
            status, response = http.BAD_REQUEST, e.as_dict()

        return jsonify_status_code(response, status)

    def get_objects(self, **kwargs):
        if current_user.is_anonymous() or not current_user.is_superuser():
            customer = self._customer()
            kwargs['id'] = customer.id

        return super(CustomerResource, self).get_objects(**kwargs)

    @property
    def _request_data(self):
        try:
            data = request.json or json.loads(request.data)
            return self.clean(data)
        except t.DataError as err:
            raise err
        except Exception:
            abort(http.BAD_REQUEST)

    def _customer(self):
        if current_user.is_anonymous():
            customer_id = session.get('customer_id')

            if customer_id is not None:
                customer = Customer.query.get(customer_id)
            else:
                abort(http.BAD_REQUEST)
        else:
            customer = current_user.customer

        return customer
