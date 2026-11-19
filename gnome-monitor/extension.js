import Gio from 'gi://Gio';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const INTERFACE = `
<node>
  <interface name="local.KittyDropdown.Monitor">
    <method name="GetPointerWorkarea">
      <arg type="i" name="x" direction="out"/>
      <arg type="i" name="y" direction="out"/>
      <arg type="i" name="width" direction="out"/>
      <arg type="i" name="height" direction="out"/>
      <arg type="i" name="stage_width" direction="out"/>
      <arg type="i" name="stage_height" direction="out"/>
    </method>
  </interface>
</node>`;

export default class KittyDropdownMonitor extends Extension {
    enable() {
        this._service = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this._service.export(Gio.DBus.session, '/local/KittyDropdown/Monitor');
    }

    disable() {
        this._service?.unexport();
        this._service = null;
    }

    GetPointerWorkarea() {
        const index = global.display.get_current_monitor();
        const workspace = global.workspace_manager.get_active_workspace();
        const {x, y, width, height} = workspace.get_work_area_for_monitor(index);
        return [x, y, width, height, global.screen_width, global.screen_height];
    }
}
