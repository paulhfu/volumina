###############################################################################
#   volumina: volume slicing and editing library
#
#       Copyright (C) 2011-2014, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the Lesser GNU General Public License
# as published by the Free Software Foundation; either version 2.1
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# See the files LICENSE.lgpl2 and LICENSE.lgpl3 for full text of the
# GNU Lesser General Public License version 2.1 and 3 respectively.
# This information is also available on the ilastik web site at:
#		   http://ilastik.org/license/
###############################################################################
from builtins import range
from PyQt5.QtWidgets import QApplication
import vtk
import numpy
import colorsys
# http://www.scipy.org/Cookbook/vtkVolumeRendering
import threading

from .meshgenerator import MeshGenerator

NOBJECTS = 256
BG_LABEL = 0
CURRENT_LABEL = 1

def makeVolumeRenderingPipeline(in_volume):
    dataImporter = vtk.vtkImageImport()

    if in_volume.dtype == numpy.uint8:
        dataImporter.SetDataScalarTypeToUnsignedChar()
    elif in_volume.dtype == numpy.uint16:
        dataImporter.SetDataScalarTypeToUnsignedShort()
    elif in_volume.dtype == numpy.int32:
        dataImporter.SetDataScalarTypeToInt()
    elif in_volume.dtype == numpy.int16:
        dataImporter.SetDataScalarTypeToShort()
    else:
        raise RuntimeError("unknown data type %r of volume" % (in_volume.dtype,))

    dataImporter.SetImportVoidPointer(in_volume, len(in_volume))
    dataImporter.SetNumberOfScalarComponents(1)
    extent = [0, in_volume.shape[2]-1, 0, in_volume.shape[1]-1, 0, in_volume.shape[0]-1]
    dataImporter.SetDataExtent(*extent)
    dataImporter.SetWholeExtent(*extent)

    alphaChannelFunc = vtk.vtkPiecewiseFunction()
    alphaChannelFunc.AddPoint(0, 0.0)
    for i in range(1, NOBJECTS):
        alphaChannelFunc.AddPoint(i, 1.0)

    colorFunc = vtk.vtkColorTransferFunction()

    volumeMapper = vtk.vtkSmartVolumeMapper()
    volumeMapper.SetInputConnection(dataImporter.GetOutputPort())

    volumeProperty = vtk.vtkVolumeProperty()
    volumeProperty.SetColor(colorFunc)
    volumeProperty.SetScalarOpacity(alphaChannelFunc)
    volumeProperty.ShadeOn()

    volume = vtk.vtkVolume()
    volume.SetMapper(volumeMapper)
    volume.SetProperty(volumeProperty)
    return dataImporter, colorFunc, volume, volumeMapper


class LabelManager(object):
    def __init__(self, n):
        self._available = set(range(1, n))
        self._used = set([])
        self._n = n

    def request(self):
        if len(self._available) == 0:
            raise RuntimeError('out of labels')
        label = min(self._available)
        self._available.remove(label)
        self._used.add(label)
        return label

    def free(self, label=None):
        if label is None:
            self._used = set([])
            self._available = set(range(1, self._n))
        elif label in self._used:
            self._used.remove(label)
            self._available.add(label)

class RenderingManager(object):
    """Encapsulates the work of adding/removing objects to the
    rendered volume and setting their colors.

    Conceptually very simple: given a volume containing integer labels
    (where zero labels represent transparent background) and a color
    map, renders the objects in the appropriate color.

    """
    def __init__(self, overview_scene):
        self._overview_scene = overview_scene
        self.labelmgr = LabelManager(NOBJECTS)
        self.ready = False
        self._cmap = {}
        self._mesh_thread = None
        self._dirty = False

        def _handle_scene_init():
            self.setup( self._overview_scene.dataShape )
            self.update()
        self._overview_scene.reinitialized.connect( _handle_scene_init )
        
    def setup(self, shape):
        shape = shape[::-1]
        self._volume = numpy.zeros(shape, dtype=numpy.uint8)
        #dataImporter, colorFunc, volume, volumeMapper = makeVolumeRenderingPipeline(self._volume)
        #self._overview_scene.set_volume(self._volume)
        #self._mapper = volumeMapper
        #self._volumeRendering = volume
        #self._dataImporter = dataImporter
        #self._colorFunc = colorFunc
        self.ready = True

    def update(self):
        assert threading.current_thread().name == 'MainThread', \
            "RenderingManager.update() must be called from the main thread to avoid segfaults."

        if not self._dirty:
            return
        self._dirty = False

        # TODO: apparently there is no fixed relation between the labels and the object names in carving.
        # TODO: Because of this the caching might not work correctly.
        new_labels = set(numpy.unique(self._volume))
        old_labels = self._overview_scene.visible_objects
        try:
            new_labels.remove(BG_LABEL)
        except KeyError:
            pass  # no error handling, because missing background does not matter

        for label in old_labels - new_labels:
            self._overview_scene.remove_object(label)

        try:
            old_labels.remove(CURRENT_LABEL)
        except KeyError:
            pass  # no error handling, because missing current label does not matter

        labels_to_add = new_labels - old_labels
        known = set(filter(self._overview_scene.has_object, labels_to_add))
        generate = labels_to_add - known
        try:
            known.remove(CURRENT_LABEL)
            generate.add(CURRENT_LABEL)
        except KeyError:
            pass  # no error handling, because missing current label does not matter

        for label in known:
            self._overview_scene.add_object(label)

        if generate:
            self._overview_scene.set_busy(True)
            self._mesh_thread = MeshGenerator(self._on_mesh_generated, self._volume, generate)

    def _on_mesh_generated(self, label, mesh):
        """
        Slot for the mesh generated signal from the MeshGenerator
        """
        assert threading.current_thread().name == 'MainThread'
        if label == 0 and mesh is None:
            self._overview_scene.set_busy(False)
        else:
            self._overview_scene.add_object(label, mesh)

    def setColor(self, label, color):
        self._cmap[label] = color

    @property
    def volume(self):
        # We store the volume in reverse-transposed form, so un-transpose it when it is accessed.
        return numpy.transpose(self._volume)

    @volume.setter
    def volume(self, value):
        # Must copy here because a reference to self._volume was stored in the pipeline (see setup())
        # store in reversed-transpose order to match the wireframe axes
        new_volume = numpy.transpose(value)
        if numpy.any(new_volume != self._volume):
            self._volume[:] = new_volume
            self._dirty = True
            self.update()

    def addObject(self, color=None):
        label = self.labelmgr.request()
        if color is None:
            color = colorsys.hsv_to_rgb(numpy.random.random(), 1.0, 1.0)
        self.setColor(label, color)
        return label

    def removeObject(self, label):
        self.labelmgr.free(label)

    def clear(self, ):
        self._volume[:] = 0
        self.labelmgr.free()

