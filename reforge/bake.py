# reforge/bake.py
import bpy
import os


def _ensure_cycles_engine(scene: bpy.types.Scene):
    """Bake works through Cycles. Temporarily switch engine to CYCLES and return previous."""
    prev_engine = scene.render.engine
    if prev_engine != "CYCLES":
        scene.render.engine = "CYCLES"
    return prev_engine


def _activate_first_uv(obj: bpy.types.Object) -> bool:
    """Ensure mesh has UVs and first UV is active (and render-active if available)."""
    uv_layers = getattr(obj.data, "uv_layers", None)
    if not uv_layers or len(uv_layers) == 0:
        return False
    try:
        obj.data.uv_layers.active_index = 0
        obj.data.uv_layers.active = obj.data.uv_layers[0]
        if hasattr(obj.data.uv_layers[0], "active_render"):
            obj.data.uv_layers[0].active_render = True
    except Exception:
        pass
    return True


def _set_active_object(context, obj: bpy.types.Object):
    """Select only this object and set it active."""
    view_layer = context.view_layer
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    view_layer.objects.active = obj


def _set_active_material_slot(obj: bpy.types.Object, mat: bpy.types.Material) -> bool:
    """Make given material active on object material slots (important for baking)."""
    mat_index = None
    for i, slot in enumerate(obj.material_slots):
        if slot.material == mat:
            mat_index = i
            break
    if mat_index is None:
        return False
    obj.active_material_index = mat_index
    obj.active_material = mat
    return True


def _find_output_node(nt: bpy.types.NodeTree):
    for n in nt.nodes:
        if n.type == "OUTPUT_MATERIAL":
            return n
    return None


def _find_principled_node(nt: bpy.types.NodeTree):
    for n in nt.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def bake_color_emit_png(
    obj: bpy.types.Object,
    mat: bpy.types.Material,
    out_abs_path: str,
    resolution: int,
    padding: int,
) -> bool:
    """
    Bake FINAL COLOR into a PNG using EMIT.
    Designed to work with complex node graphs including Ucupaint.

    Requirements:
      - obj is MESH
      - mesh has UVs
      - mat.use_nodes == True

    Returns True on success.
    """
    if not obj or obj.type != "MESH":
        print("[Reforge][Bake] Not a MESH object")
        return False
    if not mat or not mat.use_nodes or not mat.node_tree:
        print("[Reforge][Bake] Material missing or does not use nodes")
        return False

    if not _activate_first_uv(obj):
        print("[Reforge][Bake] No UVs on mesh, cannot bake")
        return False

    os.makedirs(os.path.dirname(out_abs_path), exist_ok=True)

    ctx = bpy.context
    scene = ctx.scene
    prev_engine = _ensure_cycles_engine(scene)

    # Ensure correct active object/material
    _set_active_object(ctx, obj)
    if not _set_active_material_slot(obj, mat):
        print(f"[Reforge][Bake] Material '{mat.name}' not found in object slots")
        try:
            scene.render.engine = prev_engine
        except Exception:
            pass
        return False

    # Create image datablock
    img_name = os.path.splitext(os.path.basename(out_abs_path))[0]
    img = bpy.data.images.new(
        img_name,
        width=int(resolution),
        height=int(resolution),
        alpha=True,
        float_buffer=False,
    )

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    # Find/ensure Material Output
    out_node = _find_output_node(nt)
    if out_node is None:
        out_node = nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (500, 0)

    surface_input = out_node.inputs.get("Surface")
    if surface_input is None:
        print("[Reforge][Bake] Material Output has no Surface input")
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass
        try:
            scene.render.engine = prev_engine
        except Exception:
            pass
        return False

    # --- SAVE ORIGINAL OUTPUT LINKS AS SOCKET PAIRS (robust restore) ---
    original_surface_pairs = [(l.from_socket, l.to_socket) for l in surface_input.links]

    # Disconnect surface
    for l in list(surface_input.links):
        try:
            links.remove(l)
        except Exception:
            pass

    # Create temp nodes: Emission + Image Texture
    emit_node = nodes.new("ShaderNodeEmission")
    emit_node.location = (200, 0)

    tex_node = nodes.new("ShaderNodeTexImage")
    tex_node.location = (-400, -200)
    tex_node.image = img

    # Decide what to feed into Emission Color
    principled = _find_principled_node(nt)

    from_output_socket = None
    constant_rgba = None

    if principled is not None:
        bc = principled.inputs.get("Base Color") or principled.inputs.get("Color")
        if bc is not None:
            if bc.is_linked and bc.links:
                # bc is INPUT. Take the SOURCE socket (OUTPUT) feeding it.
                try:
                    from_output_socket = bc.links[0].from_socket
                except Exception:
                    from_output_socket = None
            else:
                # Constant base color
                try:
                    constant_rgba = tuple(bc.default_value)  # RGBA
                except Exception:
                    constant_rgba = None

    # Apply color to emission
    if constant_rgba is not None:
        emit_node.inputs["Color"].default_value = constant_rgba
    elif from_output_socket is not None:
        try:
            links.new(from_output_socket, emit_node.inputs["Color"])
        except Exception as e:
            print("[Reforge][Bake] Failed to link color into emission:", e)
            emit_node.inputs["Color"].default_value = (1, 1, 1, 1)
    else:
        emit_node.inputs["Color"].default_value = (1, 1, 1, 1)

    # Link emission to output surface
    try:
        links.new(emit_node.outputs["Emission"], surface_input)
    except Exception as e:
        print("[Reforge][Bake] Failed to link emission to output:", e)

    # Make image node ACTIVE for baking (critical)
    for n in nodes:
        n.select = False
    tex_node.select = True
    nodes.active = tex_node

    # Force depsgraph update
    try:
        ctx.view_layer.update()
    except Exception:
        pass

    ok = False
    try:
        print(f"[Reforge][Bake] Baking EMIT: obj='{obj.name}', mat='{mat.name}', res={resolution}, pad={padding}")
        bpy.ops.object.bake(type='EMIT', margin=int(padding), use_clear=True)

        img.filepath_raw = out_abs_path
        img.file_format = "PNG"
        img.save()

        print(f"[Reforge][Bake] Saved: {out_abs_path}")
        ok = True

    except Exception as e:
        print(f"[Reforge][Bake][ERROR] Bake failed: {e}")
        ok = False

    finally:
        # Restore Surface links from saved socket pairs
        try:
            for l in list(surface_input.links):
                links.remove(l)
        except Exception:
            pass

        for from_sock, to_sock in original_surface_pairs:
            try:
                links.new(from_sock, to_sock)
            except Exception:
                pass

        # Remove temp nodes
        for n in (emit_node, tex_node):
            try:
                nodes.remove(n)
            except Exception:
                pass

        # Restore render engine
        try:
            scene.render.engine = prev_engine
        except Exception:
            pass

        # Remove temp image datablock (file already saved)
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass

    return ok