# system imports
import enum
import functools
import gettext
import logging
import threading

# local libraries
from nion.swift import Facade
from nion.swift import HistogramPanel
from nion.swift.model import DataItem
from nion.swift.model import DisplayItem
from nion.swift.model import Graphics
from nion.typeshed import API_1_0 as API
from nion.typeshed import UI_1_0 as UserInterface
from nion.utils import Binding
from nion.utils import Converter
from nion.utils import Event
from nion.utils import Model
from . import HardwareSourceChoice

_ = gettext.gettext


class SequenceState(enum.Enum):
    idle = 0
    scanning = 1


class ScanAcquisitionController:

    def __init__(self, api, document_controller, scan_hardware_source, camera_hardware_source):
        self.__api = api
        self.__document_controller = document_controller
        self.__scan_hardware_source = scan_hardware_source
        self.__camera_hardware_source = camera_hardware_source
        self.acquisition_state_changed_event = Event.Event()

    def start(self, sum_frames: bool) -> None:

        def create_and_display_data_item(library, data_and_metadata, scan_data_list, scan_hardware_source, camera_hardware_source):
            camera_hardware_source_id = camera_hardware_source._hardware_source.hardware_source_id

            # data_item = library.get_data_item_for_hardware_source(scan_hardware_source, channel_id=camera_hardware_source_id, processor_id="summed", create_if_needed=True, large_format=True)

            data_item = Facade.DataItem(DataItem.DataItem(large_format=True))
            library._document_model.append_data_item(data_item._data_item)
            data_item._data_item.session_id = library._document_model.session_id

            data_item.title = _("Spectrum Image {}".format(" x ".join([str(d) for d in data_and_metadata.dimensional_shape])))
            # the data item should not have any other 'clients' at this point; so setting the
            # data and metadata will immediately unload the data (and write to disk). this is important,
            # because the data (up to this point) can be shared data from the DLL.
            data_item.set_data_and_metadata(data_and_metadata)
            # assert not data_item._data_item.is_data_loaded
            # now to display it will reload the data (presumably from an HDF5 or similar on-demand format).
            document_window.display_data_item(data_item)
            for scan_data_and_metadata in scan_data_list:
                scan_channel_id = scan_data_and_metadata.metadata["hardware_source"]["channel_id"]
                scan_channel_name = scan_data_and_metadata.metadata["hardware_source"]["channel_name"]
                channel_id = camera_hardware_source_id + "_" + scan_channel_id

                # data_item = library.get_data_item_for_hardware_source(scan_hardware_source, channel_id=channel_id, create_if_needed=True)

                data_item = Facade.DataItem(DataItem.DataItem())
                library._document_model.append_data_item(data_item._data_item)
                data_item._data_item.session_id = library._document_model.session_id

                data_item.title = "{} ({})".format(_("Spectrum Image"), scan_channel_name)
                data_item.set_data_and_metadata(scan_data_and_metadata)
                document_window.display_data_item(data_item)

        document_window = self.__document_controller

        scan_hardware_source = self.__scan_hardware_source._hardware_source

        scan_frame_parameters = scan_hardware_source.get_frame_parameters(2)

        scan_hardware_source.apply_subscan(scan_frame_parameters)

        camera_hardware_source = self.__camera_hardware_source._hardware_source

        camera_frame_parameters = camera_hardware_source.get_frame_parameters(0)

        if sum_frames:
            camera_frame_parameters["processing"] = "sum_project"

        def grab_synchronized():
            self.acquisition_state_changed_event.fire(SequenceState.scanning)
            try:
                combined_data = scan_hardware_source.grab_synchronized(scan_frame_parameters=scan_frame_parameters,
                                                                       camera=camera_hardware_source,
                                                                       camera_frame_parameters=camera_frame_parameters)
                if combined_data is not None:
                    scan_data_list, camera_data_list = combined_data
                    document_window.queue_task(
                        functools.partial(create_and_display_data_item, document_window.library, camera_data_list[0],
                                          scan_data_list, self.__scan_hardware_source, self.__camera_hardware_source))
            finally:
                self.acquisition_state_changed_event.fire(SequenceState.idle)

        self.__thread = threading.Thread(target=grab_synchronized)
        self.__thread.start()

    def cancel(self) -> None:
        logging.debug("abort sequence acquisition")
        self.__scan_hardware_source._hardware_source.grab_synchronized_abort()


# see http://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size
def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Y', suffix)


def calculate_time_size(camera_hardware_source, scan_width, scan_height, camera_width, camera_height, is_summed, exposure_time):
    acquire_pixel_count = (scan_width + 2) * scan_height
    storage_pixel_count = scan_width * scan_height
    camera_frame_parameters = camera_hardware_source.get_frame_parameters(0).as_dict()
    camera_frame_parameters["acquisition_frame_count"] = acquire_pixel_count
    camera_frame_parameters["storage_frame_count"] = storage_pixel_count
    if is_summed:
        camera_frame_parameters["processing"] = "sum_project"
        storage_memory = storage_pixel_count * camera_width * 4
    else:
        storage_memory = storage_pixel_count * camera_height * camera_width * 4
    acquire_sequence_metrics = camera_hardware_source.get_acquire_sequence_metrics(camera_frame_parameters)
    acquisition_time = acquire_sequence_metrics.get("acquisition_time", exposure_time * acquire_pixel_count)  # in seconds
    acquisition_memory = acquire_sequence_metrics.get("acquisition_memory", acquire_pixel_count * camera_width * camera_height * 4)  # in bytes
    storage_memory = acquire_sequence_metrics.get("storage_memory", storage_memory)  # in bytes
    if acquisition_time > 3600:
        time_str = "{0:.1f} hours".format((int(acquisition_time) + 3599) / 3600)
    elif acquisition_time > 90:
        time_str = "{0:.1f} minutes".format((int(acquisition_time) + 59) / 60)
    else:
        time_str = "{} seconds".format(int(acquisition_time))
    if acquisition_memory != 0 and abs(storage_memory / acquisition_memory - 1) > 0.1:
        size_str = "{} ({})".format(sizeof_fmt(acquisition_memory), sizeof_fmt(storage_memory))
    else:
        size_str = sizeof_fmt(storage_memory)
    return time_str, size_str


class PanelDelegate:

    def __init__(self, api):
        self.__api = api
        self.panel_id = "scan-acquisition-panel"
        self.panel_name = _("Spectrum Imaging / 4d Scan Acquisition")
        self.panel_positions = ["left", "right"]
        self.panel_position = "right"
        self.__scan_acquisition_controller = None
        self.__acquisition_state_changed_event_listener = None
        self.__line_scan_acquisition_controller = None
        self.__eels_frame_parameters_changed_event_listener = None
        self.__camera_hardware_changed_event_listener = None
        self.__scan_hardware_changed_event_listener = None
        self.__exposure_time_ms_value_model = None
        self.__scan_hardware_source_choice = None
        self.__camera_hardware_source_choice = None
        self.__camera_width = 0
        self.__camera_height = 0

        # one or the other of the two following fields will be non-None
        self.__scan_spacing_px = 1
        self.__scan_spacing_cal = None

        # the calibration and characteristic length is updated when the context image changes
        self.__calibration = None
        self.__calibration_len = 0

        self.__scan_acquisition_preference_panel = None
        self.__target_display_item_stream = None
        self.__target_region_stream = None
        self.__target_display_item_stream_listener = None
        self.__target_region_stream_listener = None

    def create_panel_widget(self, ui, document_controller):

        self.__scan_hardware_source_choice = HardwareSourceChoice.HardwareSourceChoice(ui._ui, "scan_acquisition_hardware_source_id", lambda hardware_source: hardware_source.features.get("is_scanning"))
        self.__camera_hardware_source_choice = HardwareSourceChoice.HardwareSourceChoice(ui._ui, "scan_acquisition_camera_hardware_source_id", lambda hardware_source: hardware_source.features.get("is_camera"))
        self.__scan_acquisition_preference_panel = ScanAcquisitionPreferencePanel(self.__scan_hardware_source_choice, self.__camera_hardware_source_choice)
        # PreferencesDialog.PreferencesManager().register_preference_pane(self.__scan_acquisition_preference_panel)

        # need the target graphic where the graphic sits on a scan or subscan

        self.__target_display_item_stream = HistogramPanel.TargetDisplayItemStream(document_controller._document_window).add_ref()
        self.__target_region_stream = HistogramPanel.TargetRegionStream(self.__target_display_item_stream).add_ref()

        def matches_scan_display_item(display_item: DisplayItem.DisplayItem) -> bool:
            scan_hardware_source = self.__scan_hardware_source_choice.hardware_source
            if scan_hardware_source:
                for data_channel in scan_hardware_source.data_channels:
                    data_item_reference_key = "_".join([scan_hardware_source.hardware_source_id, data_channel.channel_id])
                    data_channel_data_item = document_controller.library.get_data_item_for_reference_key(data_item_reference_key)
                    data_channel_display_item = data_channel_data_item.display if data_channel_data_item else None
                    if data_channel_display_item and display_item == data_channel_display_item._display_item:
                        return True
                    data_item_reference_key = "_".join([scan_hardware_source.hardware_source_id, data_channel.channel_id + ".subscan"])
                    data_channel_data_item = document_controller.library.get_data_item_for_reference_key(data_item_reference_key)
                    data_channel_display_item = data_channel_data_item.display if data_channel_data_item else None
                    if data_channel_display_item and display_item == data_channel_display_item._display_item:
                        return True
            return False

        def update_context() -> None:
            # this is called when the graphic stream gets a new target graphic. if there are more than one graphics
            # selected, the graphic passed to this function will be None. the graphic stream only provides the graphic
            # so the containing display item is retrieved directly from the display item stream.
            # this method updates the text to describe what area the user has selected. it also adjusts the edit
            # fields for the appropriate graphic.
            display_item = self.__target_display_item_stream.value
            display_item = display_item if matches_scan_display_item(display_item) else None
            graphic = self.__target_region_stream.value if display_item else None
            display_data_shape = display_item.display_data_shape if display_item else None
            display_data_calibrations = display_item.displayed_dimensional_calibrations if display_item else None

            if isinstance(graphic, Graphics.LineGraphic):
                calibration = display_data_calibrations[-1]
                dimensional_shape = display_item.dimensional_shape
                length = graphic.length * dimensional_shape[-1]
                length_str = calibration.convert_to_calibrated_size_str(length, value_range=(0, display_data_shape[-1]), samples=display_data_shape[-1])
                line_str = _("Context (Line)")
                self.__roi_description.text = f"{line_str} {length_str} ({int(length)} px)"
                self.__calibration = display_data_calibrations[-1]
                self.__calibration_len = display_data_shape[-1]
                scan_str = _("Scan Dimensions (Line)")
                if self.__scan_spacing_px is not None:
                    scan_length = int(length / self.__scan_spacing_px)
                elif self.__scan_spacing_cal is not None:
                    scan_length = int(calibration.convert_to_calibrated_size(length) / self.__scan_spacing_cal)
                else:
                    scan_length = 0
                self.__scan_label_widget.text = f"{scan_str} {scan_length} px"
            elif isinstance(graphic, Graphics.RectangleGraphic):
                dimensional_shape = display_item.dimensional_shape
                width = graphic.size[1] * dimensional_shape[-1]
                height = graphic.size[0] * dimensional_shape[-1]
                width_str = display_data_calibrations[1].convert_to_calibrated_size_str(width, value_range=(0, display_data_shape[1]), samples=display_data_shape[1])
                height_str = display_data_calibrations[0].convert_to_calibrated_size_str(height, value_range=(0, display_data_shape[0]), samples=display_data_shape[0])
                rect_str = _("Context (Rectangle)")
                self.__roi_description.text = f"{rect_str} {width_str} x {height_str} ({int(width)} px x {int(height)} px)"
                self.__calibration = display_data_calibrations[-1]
                self.__calibration_len = display_data_shape[-1]
                scan_str = _("Scan Dimensions (Rectangle)")
                if self.__scan_spacing_px is not None:
                    scan_width = int(width / self.__scan_spacing_px)
                    scan_height = int(height / self.__scan_spacing_px)
                elif self.__scan_spacing_cal is not None:
                    scan_width = int(display_data_calibrations[-1].convert_to_calibrated_size(width) / self.__scan_spacing_cal)
                    scan_height = int(display_data_calibrations[-1].convert_to_calibrated_size(height) / self.__scan_spacing_cal)
                else:
                    scan_width = 0
                    scan_height = 0
                self.__scan_label_widget.text = f"{scan_str} {scan_width} x {scan_height} px"
            elif display_item and display_data_shape is not None:
                width = display_data_shape[1]
                height = display_data_shape[0]
                width_str = display_data_calibrations[1].convert_to_calibrated_size_str(width, value_range=(0, display_data_shape[1]), samples=display_data_shape[1])
                height_str = display_data_calibrations[0].convert_to_calibrated_size_str(height, value_range=(0, display_data_shape[0]), samples=display_data_shape[0])
                data_str = _("Context (Full Rect)")
                self.__roi_description.text = f"{data_str} {width_str} x {height_str} ({int(width)} x {int(height)})"
                self.__calibration = display_data_calibrations[-1]
                self.__calibration_len = display_data_shape[-1]
                scan_str = _("Scan Dimensions (Rectangle)")
                if self.__scan_spacing_px is not None:
                    scan_width = int(width / self.__scan_spacing_px)
                    scan_height = int(height / self.__scan_spacing_px)
                elif self.__scan_spacing_cal is not None:
                    scan_width = int(display_data_calibrations[-1].convert_to_calibrated_size(width) / self.__scan_spacing_cal)
                    scan_height = int(display_data_calibrations[-1].convert_to_calibrated_size(height) / self.__scan_spacing_cal)
                else:
                    scan_width = 0
                    scan_height = 0
                self.__scan_label_widget.text = f"{scan_str} {scan_width} x {scan_height} px"
            else:
                self.__roi_description.text = _("Scan context not active")
                self.__calibration = None
                self.__calibration_len = 0
                self.__scan_label_widget.text = None

            if self.__scan_spacing_px is not None and self.__calibration:
                self.__scan_spacing_pixels_widget.text = Converter.FloatToStringConverter().convert(self.__scan_spacing_px)
                self.__scan_spacing_calibrated_widget.text = self.__calibration.convert_to_calibrated_size_str(self.__scan_spacing_px, value_range=(0, self.__calibration_len), samples=self.__calibration_len)
            elif self.__scan_spacing_cal is not None and self.__calibration:
                calibrated_value_range = self.__calibration.convert_to_calibrated_size(self.__calibration_len)
                self.__scan_spacing_calibrated_widget.text = self.__calibration.convert_calibrated_size_to_str(self.__scan_spacing_cal, calibrated_value_range=(0, calibrated_value_range), samples=self.__calibration_len)
                spacing_px = self.__calibration.convert_from_calibrated_size(self.__scan_spacing_cal)
                self.__scan_spacing_pixels_widget.text = Converter.FloatToStringConverter().convert(spacing_px)
            else:
                self.__scan_spacing_pixels_widget.text = None
                self.__scan_spacing_calibrated_widget.text = None

        def new_region(graphic: Graphics.Graphic) -> None:
            update_context()

        def new_display_item(display_item: DisplayItem.DisplayItem) -> None:
            update_context()

        self.__target_display_item_stream_listener = self.__target_display_item_stream.value_stream.listen(new_display_item)
        self.__target_region_stream_listener = self.__target_region_stream.value_stream.listen(new_region)

        column = ui.create_column_widget()

        self.__style_combo_box = ui.create_combo_box_widget([_("1d (SI)"), _("2d (RI)")])
        self.__style_combo_box.current_index = 0

        acquire_sequence_button_widget = ui.create_push_button_widget(_("Acquire"))

        self.__roi_description = ui.create_label_widget()

        self.__scan_spacing_pixels_widget = ui.create_line_edit_widget()
        self.__scan_spacing_calibrated_widget = ui.create_line_edit_widget()

        self.__exposure_time_widget = ui.create_line_edit_widget()

        self.__estimate_label_widget = ui.create_label_widget()

        self.__scan_label_widget = ui.create_label_widget()

        class ComboBoxWidget:
            def __init__(self, widget):
                self.__combo_box_widget = widget

            @property
            def _widget(self):
                return self.__combo_box_widget

        camera_row = ui.create_row_widget()
        camera_row.add_spacing(12)
        camera_row.add(ComboBoxWidget(self.__camera_hardware_source_choice.create_combo_box(ui._ui)))
        camera_row.add_spacing(12)
        camera_row.add(self.__style_combo_box)
        camera_row.add_spacing(12)
        camera_row.add_stretch()

        scan_choice_row = ui.create_row_widget()
        scan_choice_row.add_spacing(12)
        scan_choice_row.add(ComboBoxWidget(self.__scan_hardware_source_choice.create_combo_box(ui._ui)))
        scan_choice_row.add_spacing(12)
        scan_choice_row.add_stretch()

        roi_size_row = ui.create_row_widget()
        roi_size_row.add_spacing(12)
        roi_size_row.add(self.__roi_description)
        roi_size_row.add_spacing(12)
        roi_size_row.add_stretch()

        scan_spacing_pixels_row = ui.create_row_widget()
        scan_spacing_pixels_row.add_spacing(12)
        scan_spacing_pixels_row.add(ui.create_label_widget("Scan Spacing (pixels)"))
        scan_spacing_pixels_row.add_spacing(12)
        scan_spacing_pixels_row.add(self.__scan_spacing_pixels_widget)
        scan_spacing_pixels_row.add_spacing(12)
        scan_spacing_pixels_row.add_stretch()

        scan_spacing_calibrated_row = ui.create_row_widget()
        scan_spacing_calibrated_row.add_spacing(12)
        scan_spacing_calibrated_row.add(ui.create_label_widget("Scan Spacing (nm)"))
        scan_spacing_calibrated_row.add_spacing(12)
        scan_spacing_calibrated_row.add(self.__scan_spacing_calibrated_widget)
        scan_spacing_calibrated_row.add_spacing(12)
        scan_spacing_calibrated_row.add_stretch()

        eels_exposure_row = ui.create_row_widget()
        eels_exposure_row.add_spacing(12)
        eels_exposure_row.add(ui.create_label_widget("Camera Exposure Time (ms)"))
        eels_exposure_row.add_spacing(12)
        eels_exposure_row.add(self.__exposure_time_widget)
        eels_exposure_row.add_spacing(12)
        eels_exposure_row.add_stretch()

        scan_row = ui.create_row_widget()
        scan_row.add_spacing(12)
        scan_row.add(self.__scan_label_widget)
        scan_row.add_stretch()

        estimate_row = ui.create_row_widget()
        estimate_row.add_spacing(12)
        estimate_row.add(self.__estimate_label_widget)
        estimate_row.add_stretch()

        acquire_sequence_button_row = ui.create_row_widget()
        acquire_sequence_button_row.add(acquire_sequence_button_widget)
        acquire_sequence_button_row.add_stretch()

        if self.__scan_hardware_source_choice.hardware_source_count > 1:
            column.add_spacing(8)
            column.add(scan_choice_row)
        column.add_spacing(8)
        column.add(camera_row)
        column.add_spacing(8)
        column.add(roi_size_row)
        column.add_spacing(8)
        column.add(scan_spacing_pixels_row)
        column.add_spacing(8)
        column.add(scan_spacing_calibrated_row)
        column.add_spacing(8)
        column.add(eels_exposure_row)
        column.add_spacing(8)
        column.add(scan_row)
        column.add_spacing(8)
        column.add(estimate_row)
        column.add_spacing(8)
        column.add(acquire_sequence_button_row)
        column.add_spacing(8)
        column.add_stretch()

        def camera_hardware_source_changed(hardware_source):
            self.disconnect_camera_hardware_source()
            if hardware_source:
                self.connect_camera_hardware_source(hardware_source)

        self.__camera_hardware_changed_event_listener = self.__camera_hardware_source_choice.hardware_source_changed_event.listen(camera_hardware_source_changed)
        camera_hardware_source_changed(self.__camera_hardware_source_choice.hardware_source)

        def style_current_item_changed(current_item):
            self.__update_estimate()

        self.__style_combo_box.on_current_item_changed = style_current_item_changed

        def scan_spacing_pixels_changed(text):
            spacing = Converter.FloatToStringConverter().convert_back(text)
            if spacing > 0 and self.__calibration:
                if self.__scan_spacing_px != spacing:
                    self.__scan_spacing_px = spacing
                    self.__scan_spacing_cal = None
                    update_context()
                    self.__scan_spacing_pixels_widget.select_all()
            else:
                self.__scan_spacing_px = None
                self.__scan_spacing_cal = None

        def scan_spacing_calibrated_changed(text):
            spacing = Converter.FloatToStringConverter().convert_back(text)
            if spacing > 0 and self.__calibration:
                if self.__scan_spacing_cal != spacing:
                    self.__scan_spacing_cal = spacing
                    self.__scan_spacing_px = None
                    update_context()
                    self.__scan_spacing_calibrated_widget.select_all()
            else:
                self.__scan_spacing_px = None
                self.__scan_spacing_cal = None

        self.__scan_spacing_pixels_widget.on_editing_finished = scan_spacing_pixels_changed
        self.__scan_spacing_calibrated_widget.on_editing_finished = scan_spacing_calibrated_changed

        self.__scan_spacing_px = 1
        self.__scan_spacing_cal = None

        new_region(self.__target_region_stream.value)

        def acquisition_state_changed(acquisition_state: SequenceState) -> None:

            async def update_button_text(text: str) -> None:
                acquire_sequence_button_widget.text = text

            if acquisition_state == SequenceState.idle:
                self.__scan_acquisition_controller = None
                self.__acquisition_state_changed_event_listener.close()
                self.__acquisition_state_changed_event_listener = None
                document_controller._document_window.event_loop.create_task(update_button_text(_("Acquire")))
            else:
                document_controller._document_window.event_loop.create_task(update_button_text(_("Cancel")))

        def acquire_sequence() -> None:
            if self.__scan_acquisition_controller:
                if self.__scan_acquisition_controller:
                    self.__scan_acquisition_controller.cancel()
            else:
                if self.__scan_hardware_source_choice.hardware_source:
                    scan_hardware_source = self.__api.get_hardware_source_by_id(self.__scan_hardware_source_choice.hardware_source.hardware_source_id, version="1.0")
                else:
                    scan_hardware_source = None

                if self.__camera_hardware_source_choice.hardware_source:
                    camera_hardware_source = self.__api.get_hardware_source_by_id(self.__camera_hardware_source_choice.hardware_source.hardware_source_id, version="1.0")
                else:
                    camera_hardware_source = None

                if scan_hardware_source and camera_hardware_source:
                    self.__scan_acquisition_controller = ScanAcquisitionController(self.__api, document_controller, scan_hardware_source, camera_hardware_source)
                    self.__acquisition_state_changed_event_listener = self.__scan_acquisition_controller.acquisition_state_changed_event.listen(acquisition_state_changed)
                    self.__scan_acquisition_controller.start(self.__style_combo_box.current_index == 0)

        acquire_sequence_button_widget.on_clicked = acquire_sequence

        self.__update_estimate()

        return column

    def __update_estimate(self):
        if self.__exposure_time_ms_value_model:
            camera_hardware_source = self.__camera_hardware_source_choice.hardware_source
            camera_width = self.__camera_width
            camera_height = self.__camera_height
            scan_width = 1  # self.__scan_width_model.value
            scan_height = 1  # self.__scan_height_model.value
            is_summed = self.__style_combo_box.current_index == 0
            exposure_time = self.__exposure_time_ms_value_model.value / 1000
            time_str, size_str = calculate_time_size(camera_hardware_source, scan_width, scan_height, camera_width, camera_height, is_summed, exposure_time)
            self.__estimate_label_widget.text = "{0} / {1}".format(time_str, size_str)
        else:
            self.__estimate_label_widget.text = None

    def connect_camera_hardware_source(self, camera_hardware_source):

        self.__exposure_time_ms_value_model = Model.PropertyModel()

        def update_exposure_time_ms(exposure_time_ms):
            if exposure_time_ms > 0:
                frame_parameters = camera_hardware_source.get_frame_parameters(0)
                frame_parameters.exposure_ms = exposure_time_ms
                camera_hardware_source.set_frame_parameters(0, frame_parameters)
            self.__update_estimate()

        self.__exposure_time_ms_value_model.on_value_changed = update_exposure_time_ms

        exposure_time_ms_value_binding = Binding.PropertyBinding(self.__exposure_time_ms_value_model, "value", converter=Converter.FloatToStringConverter("{0:.1f}"))

        def eels_profile_parameters_changed(profile_index, frame_parameters):
            if profile_index == 0:
                expected_dimensions = camera_hardware_source.get_expected_dimensions(frame_parameters.binning)
                self.__camera_width = expected_dimensions[1]
                self.__camera_height = expected_dimensions[0]
                self.__exposure_time_ms_value_model.value = frame_parameters.exposure_ms
                self.__update_estimate()

        self.__eels_frame_parameters_changed_event_listener = camera_hardware_source.frame_parameters_changed_event.listen(eels_profile_parameters_changed)

        eels_profile_parameters_changed(0, camera_hardware_source.get_frame_parameters(0))

        self.__exposure_time_widget._widget.bind_text(exposure_time_ms_value_binding)  # the widget will close the binding

    def disconnect_camera_hardware_source(self):
        self.__exposure_time_widget._widget.unbind_text()
        if self.__eels_frame_parameters_changed_event_listener:
            self.__eels_frame_parameters_changed_event_listener.close()
            self.__eels_frame_parameters_changed_event_listener = None
        if self.__exposure_time_ms_value_model:
            self.__exposure_time_ms_value_model.close()
            self.__exposure_time_ms_value_model = None

    def close(self):
        if self.__eels_frame_parameters_changed_event_listener:
            self.__eels_frame_parameters_changed_event_listener.close()
            self.__eels_frame_parameters_changed_event_listener = None
        if self.__camera_hardware_changed_event_listener:
            self.__camera_hardware_changed_event_listener.close()
            self.__camera_hardware_changed_event_listener = None
        if self.__scan_hardware_changed_event_listener:
            self.__scan_hardware_changed_event_listener.close()
            self.__scan_hardware_changed_event_listener = None
        if self.__scan_hardware_source_choice:
            self.__scan_hardware_source_choice.close()
            self.__scan_hardware_source_choice = None
        if self.__camera_hardware_source_choice:
            self.__camera_hardware_source_choice.close()
            self.__camera_hardware_source_choice = None
        if self.__scan_acquisition_preference_panel:
            # PreferencesDialog.PreferencesManager().unregister_preference_pane(self.__scan_acquisition_preference_panel)
            self.__scan_acquisition_preference_panel = None
        if self.__target_display_item_stream_listener:
            self.__target_display_item_stream_listener.close()
            self.__target_display_item_stream_listener = None
        if self.__target_region_stream_listener:
            self.__target_region_stream_listener.close()
            self.__target_region_stream_listener = None
        if self.__target_region_stream:
            self.__target_region_stream.remove_ref()
        if self.__target_display_item_stream:
            self.__target_display_item_stream.remove_ref()


class ScanAcquisitionPreferencePanel:
    def __init__(self, scan_hardware_source_choice, other_hardware_source_choice):
        self.identifier = "scan_acquisition"
        self.label = _("Spectrum Imaging / 4d Scan Acquisition")
        self.__scan_hardware_source_choice = scan_hardware_source_choice
        self.__camera_hardware_source_choice = other_hardware_source_choice

    def build(self, ui, **kwargs):
        scan_hardware_source_combo_box = self.__scan_hardware_source_choice.create_combo_box(ui)
        other_hardware_source_combo_box = self.__camera_hardware_source_choice.create_combo_box(ui)
        row = ui.create_row_widget()
        row.add(ui.create_label_widget(_("Scan Device")))
        row.add_spacing(12)
        row.add(scan_hardware_source_combo_box)
        row.add_spacing(12)
        row.add(other_hardware_source_combo_box)
        return row


class ScanAcquisitionExtension:

    # required for Swift to recognize this as an extension class.
    extension_id = "nion.instrumentation-kit.scan-acquisition"

    def __init__(self, api_broker):
        # grab the api object.
        api = api_broker.get_api(version=API.version, ui_version=UserInterface.version)
        # be sure to keep a reference or it will be closed immediately.
        self.__panel_ref = api.create_panel(PanelDelegate(api))

    def close(self):
        # close will be called when the extension is unloaded. in turn, close any references so they get closed. this
        # is not strictly necessary since the references will be deleted naturally when this object is deleted.
        # self.__menu_item_ref.close()
        # self.__menu_item_ref = None
        self.__panel_ref.close()
        self.__panel_ref = None
