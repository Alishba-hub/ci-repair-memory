# Copyright 2021-2025 Avaiga Private Limited
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

import re

from apispec import APISpec, BasePlugin, yaml_utils
from apispec.exceptions import APISpecError
from apispec.ext.marshmallow import MarshmallowPlugin
from flask import Blueprint, current_app, jsonify, render_template
from flask.views import MethodView

_RE_URL = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


class FlaskPlugin(BasePlugin):
    """Small Flask apispec plugin that avoids importing apispec_webframeworks."""

    @staticmethod
    def flaskpath2openapi(path):
        return _RE_URL.sub(r"{\1}", path)

    @staticmethod
    def _rule_for_view(view, app=None):
        if app is None:
            app = current_app

        view_funcs = app.view_functions
        endpoint = next((ept for ept, view_func in view_funcs.items() if view_func == view), None)

        if not endpoint:
            raise APISpecError("Could not find endpoint for view {0}".format(view))

        # WARNING: Assume 1 rule per view function for now
        return app.url_map._rules_by_endpoint[endpoint][0]

    def path_helper(self, operations, *, view, app=None, **kwargs):
        rule = self._rule_for_view(view, app=app)
        operations.update(yaml_utils.load_operations_from_docstring(view.__doc__))

        if hasattr(view, "view_class") and issubclass(view.view_class, MethodView):
            for method in view.methods:
                if method in rule.methods:
                    method_name = method.lower()
                    operations[method_name] = yaml_utils.load_yaml_from_docstring(
                        getattr(view.view_class, method_name).__doc__
                    )

        return self.flaskpath2openapi(rule.rule)


class FlaskRestfulPlugin(FlaskPlugin):
    """Small plugin override to handle flask-restful resources"""

    @staticmethod
    def _rule_for_view(view, app=None):
        view_funcs = app.view_functions
        endpoint = None

        for ept, view_func in view_funcs.items():
            if hasattr(view_func, "view_class"):
                view_func = view_func.view_class

            if view_func == view:
                endpoint = ept

        if not endpoint:
            raise APISpecError("Could not find endpoint for view {0}".format(view))

        # WARNING: Assume 1 rule per view function for now
        return app.url_map._rules_by_endpoint[endpoint][0]


class APISpecExt:
    """Very simple and small extension to use apispec with this API as a flask extension"""

    def __init__(self, app=None, **kwargs):
        self.spec = None

        if app is not None:
            self.init_app(app, **kwargs)

    def init_app(self, app, **kwargs):
        app.config.setdefault("APISPEC_TITLE", "Taipy Rest")
        app.config.setdefault("APISPEC_VERSION", "1.0.0")
        app.config.setdefault("OPENAPI_VERSION", "3.0.2")
        app.config.setdefault("SWAGGER_JSON_URL", "/swagger.json")
        app.config.setdefault("SWAGGER_UI_URL", "/swagger-ui")
        app.config.setdefault("OPENAPI_YAML_URL", "/openapi.yaml")
        app.config.setdefault("REDOC_UI_URL", "/redoc-ui")
        app.config.setdefault("SWAGGER_URL_PREFIX", None)

        self.spec = APISpec(
            title=app.config["APISPEC_TITLE"],
            version=app.config["APISPEC_VERSION"],
            openapi_version=app.config["OPENAPI_VERSION"],
            plugins=[MarshmallowPlugin(), FlaskRestfulPlugin()],
            **kwargs,
        )

        blueprint = Blueprint(
            "swagger",
            __name__,
            template_folder="./templates",
            url_prefix=app.config["SWAGGER_URL_PREFIX"],
        )

        blueprint.add_url_rule(app.config["SWAGGER_JSON_URL"], "swagger_json", self.swagger_json)
        blueprint.add_url_rule(app.config["SWAGGER_UI_URL"], "swagger_ui", self.swagger_ui)
        blueprint.add_url_rule(app.config["OPENAPI_YAML_URL"], "openapi_yaml", self.openapi_yaml)
        blueprint.add_url_rule(app.config["REDOC_UI_URL"], "redoc_ui", self.redoc_ui)

        app.register_blueprint(blueprint)

    def swagger_json(self):
        return jsonify(self.spec.to_dict())

    def swagger_ui(self):
        return render_template("swagger.j2")

    def openapi_yaml(self):
        # Manually inject ReDoc's Authentication legend, then remove it
        self.spec.tag(
            {
                "name": "authentication",
                "x-displayName": "Authentication",
                "description": "<SecurityDefinitions />",
            }
        )
        redoc_spec = self.spec.to_yaml()
        self.spec._tags.pop(0)
        return redoc_spec

    def redoc_ui(self):
        return render_template("redoc.j2")
