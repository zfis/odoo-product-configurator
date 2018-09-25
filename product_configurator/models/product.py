from lxml import etree

from odoo.exceptions import ValidationError
from odoo import models, fields, api, _
from odoo.addons import decimal_precision as dp
from mako.template import Template
from mako.runtime import Context
from odoo.tools.safe_eval import safe_eval
from io import StringIO
import logging

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    config_ok = fields.Boolean(string='Can be Configured')

    config_line_ids = fields.One2many(
        comodel_name='product.config.line',
        inverse_name='product_tmpl_id',
        string="Attribute Dependencies"
    )

    config_image_ids = fields.One2many(
        comodel_name='product.config.image',
        inverse_name='product_tmpl_id',
        string='Configuration Images'
    )

    config_step_line_ids = fields.One2many(
        comodel_name='product.config.step.line',
        inverse_name='product_tmpl_id',
        string='Configuration Lines'
    )

    mako_tmpl_name = fields.Text(
        string='Variant name',
        help="Generate Name based on Mako Template"
    )

    @api.multi
    def get_product_attribute_values_action(self):
        self.ensure_one()
        action = self.env.ref(
            'product.product_attribute_value_action').read()[0]
        value_ids = self.attribute_line_ids.mapped('value_ids').ids
        action['domain'] = [('id', 'in', value_ids)]
        context = safe_eval(action['context'], {'active_id': self.id})
        context.update({'active_id': self.id})
        action['context'] = context
        return action

    @api.multi
    @api.constrains('attribute_line_ids')
    def _check_default_values(self):
        """Validate default values set on the product template"""
        default_val_ids = self.attribute_line_ids.filtered(
            lambda l: l.default_val).mapped('default_val').ids

        # TODO: Remove if cond when PR with raise error on github is merged
        cfg_session_obj = self.env['product.config.session']
        valid_conf = cfg_session_obj.validate_configuration(
            value_ids=default_val_ids, final=False
        )
        if not valid_conf:
            raise ValidationError(
                _('Default values provided generate an invalid configuration')
            )

    @api.multi
    @api.constrains('config_line_ids')
    def _check_default_value_domains(self):
        try:
            self._check_default_values()
        except ValidationError:
            raise ValidationError(
                _('Restrictions added make the current default values '
                  'generate an invalid configuration')
            )

    @api.multi
    def toggle_config(self):
        for record in self:
            record.config_ok = not record.config_ok

    @api.multi
    def create_variant_ids(self):
        """ Prevent configurable products from creating variants as these serve
            only as a template for the product configurator"""
        templates = self.filtered(lambda t: not t.config_ok)
        if not templates:
            return None
        return super(ProductTemplate, templates).create_variant_ids()

    @api.multi
    def unlink(self):
        """ Prevent the removal of configurable product templates
            from variants"""
        for template in self:
            variant_unlink = self.env.context.get('unlink_from_variant', False)
            if template.config_ok and variant_unlink:
                self -= template
        res = super(ProductTemplate, self).unlink()
        return res

    @api.multi
    def copy(self, default=None):
        res = super(ProductTemplate, self).copy(default=default)
        return res

    @api.multi
    def configure_product(self):
        wizard_vals = {
            'product_tmpl_id': self.id
        }
        wizard = self.env['product.configurator'].create(wizard_vals)
        return wizard.action_next_step()


class ProductProduct(models.Model):
    _inherit = 'product.product'
    _rec_name = 'config_name'

    def _get_conversions_dict(self):
        conversions = {
            'float': float,
            'int': int
        }
        return conversions

    @api.constrains('attribute_value_ids')
    def _check_duplicate_product(self):
        if not self.config_ok:
            return None

        # At the moment, I don't have enough confidence with my understanding
        # of binary attributes, so will leave these as not matching...
        # In theory, they should just work, if they are set to "non search"
        # in custom field def!
        # TODO: Check the logic with binary attributes
        if not self.value_custom_ids.filtered(lambda cv: cv.attachment_ids):
            config_session_obj = self.env['product.config.session']
            custom_vals = {
                cv.attribute_id.id: cv.value
                for cv in self.value_custom_ids
            }
            duplicates = config_session_obj.search_variant(
                product_tmpl_id=self.product_tmpl_id.id,
                value_ids=self.attribute_value_ids.ids,
                custom_vals=custom_vals
            ).filtered(lambda p: p.id != self.id)

            if duplicates:
                raise ValidationError(
                    _("Configurable Products cannot have duplicates "
                      "(identical attribute values)")
                )

    @api.depends('attribute_value_ids.price_ids.price_extra',
                 'attribute_value_ids.price_ids.product_tmpl_id')
    def _compute_product_price_extra(self):
        """Compute price of configurable products as sum of products related
        to attribute values picked"""
        standard_products = self.filtered(lambda x: not x.config_ok)
        configurable_products = self - standard_products
        if standard_products:
            prices = super(ProductProduct, self)._compute_product_price_extra()

        conversions = self._get_conversions_dict()
        for product in configurable_products:
            lst_price = product.product_tmpl_id.lst_price
            value_ids = product.attribute_value_ids.ids
            # TODO: Merge custom values from products with cfg session
            # and use same method to retrieve parsed custom val dict
            custom_vals = {}
            for val in product.value_custom_ids:
                custom_type = val.attribute_id.custom_type
                if custom_type in conversions:
                    try:
                        custom_vals[val.attribute_id.id] = conversions[
                            custom_type](val.value)
                    except Exception:
                        raise ValidationError(
                            _("Could not convert custom value '%s' to '%s' on "
                              "product variant: '%s'" % (val.value,
                                                         custom_type,
                                                         product.display_name))
                        )
                else:
                    custom_vals[val.attribute_id.id] = val.value
            prices = self.env['product.config.session'].get_cfg_price(
                value_ids, custom_vals)
            product.price_extra = prices['total'] - prices['taxes'] - lst_price

    @api.model
    def _get_config_name(self):
        return self.name

    @api.model
    def _get_mako_context(self, buf):
        return Context(
            buf, product=self,
            attribute_values=self.attribute_value_ids,
            steps=self.product_tmpl_id.config_step_line_ids,
            template=self.product_tmpl_id)

    @api.model
    def _get_mako_tmpl_name(self):
        if self.mako_tmpl_name:
            try:
                mytemplate = Template(self.mako_tmpl_name or '')
                buf = StringIO()
                ctx = self._get_mako_context(buf)
                mytemplate.render_context(ctx)
                return buf.getvalue()
            except Exception:
                _logger.error(
                    _("Error while calculating mako product name: %s") %
                    self.display_name)
        return self.display_name

    config_name = fields.Char(
        string="Name",
        size=256,
        compute='_compute_name',
    )
    value_custom_ids = fields.One2many(
        comodel_name='product.attribute.value.custom',
        inverse_name='product_id',
        string='Custom Values',
        readonly=True
    )
    price_extra = fields.Float(
        compute='_compute_product_price_extra',
        string='Variant Extra Price',
        help="This is the sum of the extra price of all attributes",
        digits=dp.get_precision('Product Price')
    )

    @api.multi
    def get_product_attribute_values_action(self):
        self.ensure_one()
        action = self.env.ref(
            'product.product_attribute_value_action').read()[0]
        value_ids = self.attribute_value_ids.ids
        action['domain'] = [('id', 'in', value_ids)]
        context = safe_eval(
            action['context'],
            {'active_id': self.product_tmpl_id.id}
        )
        context.update({'active_id': self.product_tmpl_id.id})
        action['context'] = context
        return action

    @api.multi
    def _check_attribute_value_ids(self):
        """ Removing multi contraint attribute to enable multi selection. """
        return True

    _constraints = [
        (_check_attribute_value_ids, None, ['attribute_value_ids'])
    ]

    @api.model
    def fields_view_get(self, view_id=None, view_type='form',
                        toolbar=False, submenu=False):
        """ For configurable products switch the name field with the config_name
            so as to keep the view intact in whatever form it is at the moment
            of execution and not duplicate the original just for the sole
            purpose of displaying the proper name"""
        res = super(ProductProduct, self).fields_view_get(
            view_id=view_id, view_type=view_type,
            toolbar=toolbar, submenu=submenu
        )
        if self.env.context.get('default_config_ok'):
            xml_view = etree.fromstring(res['arch'])
            xml_name = xml_view.xpath("//field[@name='name']")
            xml_label = xml_view.xpath("//label[@for='name']")
            if xml_name:
                xml_name[0].attrib['name'] = 'config_name'
                if xml_label:
                    xml_label[0].attrib['for'] = 'config_name'
                view_obj = self.env['ir.ui.view']
                xarch, xfields = view_obj.postprocess_and_fields(self._name,
                                                                 xml_view,
                                                                 view_id)
                res['arch'] = xarch
                res['fields'] = xfields
        return res

    @api.multi
    def unlink(self):
        """ Signal unlink from product variant through context so
            removal can be stopped for configurable templates """
        ctx = dict(self.env.context, unlink_from_variant=True)
        self.env.context = ctx
        return super(ProductProduct, self).unlink()

    @api.multi
    def _compute_name(self):
        """ Compute the name of the configurable products and use template
            name for others"""
        for product in self:
            if product.config_ok:
                product.config_name = product._get_config_name()
            else:
                product.config_name = product.name

    @api.multi
    def reconfigure_product(self):
        """ Creates and launches a product configurator wizard with a linked
        template and variant in order to re-configure a existing product. It is
        esetially a shortcut to pre-fill configuration data of a variant"""

        wizard_obj = self.env['product.configurator']
        wizard = wizard_obj.create({
            'product_id': self.id,
        })
        return wizard.action_next_step()
