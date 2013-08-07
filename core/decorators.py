# -*- encoding: utf-8 -*-
from __future__ import absolute_import
import trafaret as t
from functools import wraps
from flask import abort, current_app, request
from flask.ext.babel import get_locale
from flask.ext.security import current_user

from . import db, http
from .utils import jsonify_status_code, plural_underscored


def api_resource(bp, endpoint, pk_def):
    pk = pk_def.keys()[0]
    pk_type = pk_def[pk] and pk_def[pk].__name__ or None
    # building url from the endpoint
    url = "/{}/".format(endpoint)
    collection_methods = ['GET', 'POST']
    item_methods = ['GET', 'PUT', 'DELETE']

    def wrapper(resource_class):
        resource = resource_class().as_view(endpoint)
        bp.add_url_rule(url, view_func=resource, methods=collection_methods)
        if pk_type is None:
            url_rule = "{}<{}>".format(url, pk)
        else:
            url_rule = "{}<{}:{}>".format(url, pk_type, pk)

        bp.add_url_rule(url_rule, view_func=resource, methods=item_methods)
        return resource_class

    return wrapper


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated():
            abort(http.UNAUTHORIZED)
        return fn(*args, **kwargs)

    return wrapper


def multilingual(cls):
    from sqlalchemy.ext.hybrid import hybrid_property
    from flamaster.core.models import CRUDMixin

    def _get_locale():
        lang = unicode(get_locale())
        try:
            if '_lang' in request.args:
                lang = unicode(request.args['_lang'])
            elif request.json and '_lang' in request.json:
                lang = unicode(request.json['_lang'])
        except RuntimeError:
            pass

        if lang is None:
            lang = unicode(current_app.conf['BABEL_DEFAULT_LOCALE'])

        return lang

    def create_property(cls, localized, columns, field):

        def getter(self):
            lang = _get_locale()
            instance = localized.query.filter_by(parent_id=self.id,
                                                 locale=lang).first()
            return instance and getattr(instance, field) or None

        def setter(self, value):
            lang = _get_locale()
            from_db = localized.query.filter_by(parent_id=self.id,
                                                locale=lang).first()

            instance = from_db or localized(parent=self, locale=lang)
            setattr(instance, field, value)
            instance.save()

        def expression(self):
            lang = _get_locale()
            return db.Query(columns[field]) \
                .filter(localized.parent_id == self.id,
                        localized.locale == lang).as_scalar()

        setattr(cls, field, hybrid_property(getter, setter, expr=expression))

    def closure(cls):
        lang = _get_locale()
        class_name = cls.__name__ + 'Localized'
        tablename = plural_underscored(class_name)

        if db.metadata.tables.get(tablename) is not None:
            return cls

        cls_columns = cls.__table__.get_children()
        columns = dict([(c.name, c.copy()) for c in cls_columns if isinstance(c.type, (db.Unicode, db.UnicodeText))])
        localized_names = columns.keys()

        columns.update({
            'parent_id': db.Column(db.Integer,
                                   db.ForeignKey(cls.__tablename__ + '.id',
                                                 ondelete="CASCADE",
                                                 onupdate="CASCADE"),
                                   nullable=True),
            'parent': db.relationship(cls, backref='localized_ref'),
            'locale': db.Column(db.Unicode(255), default=lang, index=True)
        })

        cls_localized = type(class_name, (db.Model, CRUDMixin), columns)

        for field in localized_names:
            create_property(cls, cls_localized, columns, field)

        return cls

    return closure(cls)


def method_wrapper(http_status):
    def method_catcher(meth):
        @wraps(meth)
        def wrapper(*args):
            try:
                data = request.json or abort(http.BAD_REQUEST)
                return jsonify_status_code(meth(*args, data=data), http_status)
            except t.DataError as e:
                return jsonify_status_code(e.as_dict(), http.BAD_REQUEST)
        return wrapper
    return method_catcher


class ClassProperty(property):
    def __init__(self, method, *args, **kwargs):
        method = classmethod(method)
        return super(ClassProperty, self).__init__(method, *args, **kwargs)

    def __get__(self, cls, owner):
        return self.fget.__get__(None, owner)()


classproperty = ClassProperty


def crm_language(serialize):
    def wrapper(obj, instance, include=None):
        data = serialize(obj, instance, include)

        if 'i18n' in obj.model.__dict__:
            i18n = filter(lambda field: field in data, obj.model.i18n)
            for field in i18n:
                field_val = instance.get(field)
                if field_val is not None \
                    and type(field_val[instance._lang]) not in (unicode, str):
                    data[field] = ''
                else:
                    field_val = data[field]
                    if type(field_val) not in (unicode, str) and \
                        instance._lang not in field_val:
                        data[field] = ''

        return data

    return wrapper
