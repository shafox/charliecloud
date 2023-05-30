import json
import os.path

import charliecloud as ch
import build_cache as bu
import image as im
import registry as rg
import version

## Globals ##

upload_cache = None

## Main ##

def main(cli):
   src_ref = im.Reference(cli.source_ref)
   ch.INFO("pushing image:   %s" % src_ref)
   image = im.Image(src_ref, cli.image)
   # FIXME: validate it’s an image using Megan’s new function (PR #908)
   if (not os.path.isdir(image.unpack_path)):
      if (cli.image is not None):
         ch.FATAL("can’t push: %s does not appear to be an image" % cli.image)
      else:
         ch.FATAL("can’t push: no image %s" % src_ref)
   if (cli.image is not None):
      ch.INFO("image path:      %s" % image.unpack_path)
   else:
      ch.VERBOSE("image path:      %s" % image.unpack_path)
   if (cli.dest_ref is not None):
      dst_ref = im.Reference(cli.dest_ref)
      ch.INFO("destination:     %s" % dst_ref)
   else:
      dst_ref = im.Reference(cli.source_ref)

   global upload_cache
   if (cli.ulcache and isinstance(bu.cache, bu.Disabled_Cache)):
      ch.FATAL('build cache disabled')
   elif (cli.ulcache):
      upload_cache = True
   up = Image_Pusher(image, dst_ref)
   up.push()
   ch.done_notify()


## Classes ##


class Image_Pusher:

   __slots__ = ("config",     # sequence of bytes
                "dst_ref",    # destination of upload
                "git_hash",   # image HEAD git hash
                "image",      # Image object we are uploading
                "layers",     # list of (digest, .tar.gz path) to push, lowest first
                "manifest",   # sequence of bytes
                "registry")   # destination registry

   def __init__(self, image, dst_ref):
      self.config = None
      self.dst_ref = dst_ref
      self.git_hash = None
      self.image = image
      self.layers = None
      self.manifest = None
      self.registry = None
      (sid, git_hash) = bu.cache.find_image(self.image)
      if (git_hash is not None):
         self.git_hash = git_hash

   @property
   def path_config(self):
      file_id = self.git_hash
      if (not file_id):
         file_id = self.image.ref_for_path
      return ch.storage.upload_cache // str(file_id + ".config.json")

   @property
   def path_manifest(self):
      file_id = self.git_hash
      if (not file_id):
         file_id = self.image.ref_for_path
      return ch.storage.upload_cache // str(file_id + ".manifest.json")

   @classmethod
   def config_new(class_):
      "Return an empty config, ready to be filled in."
      # FIXME: URL of relevant docs?
      # FIXME: tidy blank/empty fields?
      return { "architecture": ch.arch_host_get(),
               "charliecloud_version": version.VERSION,
               "comment": "pushed with Charliecloud",
               "config": {},
               "container_config": {},
               "created": ch.now_utc_iso8601(),
               "history": [],
               "os": "linux",
               "rootfs": { "diff_ids": [], "type": "layers" },
               "weirdal": "yankovic" }

   @classmethod
   def manifest_new(class_):
      "Return an empty manifest, ready to be filled in."
      return { "schemaVersion": 2,
               "mediaType": rg.TYPES_MANIFEST["docker2"],
               "config": { "mediaType": rg.TYPE_CONFIG,
                           "size": None,
                           "digest": None },
               "layers": [],
               "weirdal": "yankovic" }

   def cleanup(self):
      if (upload_cache is None):
         ch.INFO("cleaning up")
         # Delete the tarballs since we can’t yet cache them.
         for (_, tar_c) in self.layers:
            ch.VERBOSE("deleting tarball: %s" % tar_c)
            tar_c.unlink_()

   def layers_from_json(self, manifest, error_fatal=True):
      """Return a list of (digest, tar_c) layer tuples read from manifest. If
         error_fatal exit with error for problems; otherwise, return None."""
      version = self.image.schema_ver_from_json(manifest, error_fatal)
      layer_hashes = self.image.layer_hash_from_json(manifest, version,
                                                     error_fatal)
      if (version is None or manifest is None or layer_hashes is None):
         return None
      tars_c = list()
      for digest in layer_hashes:
         path_c = ch.storage.upload_cache // str(digest + ".tar.gz")
         tars_c.append((digest, path_c))
      return tars_c

   def prepare(self):
      """Prepare self.image for pushing to self.dst_ref. Return tuple: (list
         of gzipped layer tarball paths, config as a sequence of bytes,
         manifest as a sequence of bytes)."""

      # Initializing an HTTP instance for the registry and doing a 'GET'
      # request right out the gate ensures the user needs to authenticate
      # before we prepare the image for upload (#1426).
      self.registry = rg.HTTP(self.dst_ref)
      self.registry.request("GET", self.registry._url_base)

      config = None
      manifest = None
      layers = None

      if (upload_cache is not None):
         # Check for previously prepared; if they exist, use them.
         ch.VERBOSE("--ulcache: checking for previously prepared files")
         config = self.path_config.read_to_json('config', error_fatal=False)
         manifest = self.path_manifest.read_to_json('manifest',
                                                      error_fatal=False)
         layers = self.layers_from_json(manifest, error_fatal=False)
         layers = self.layers_from_json(manifest, error_fatal=False)

      # If cache is disabled, or one or more previously prepared files is
      # missing; create new ones.
      if (config is None or manifest is None or layers is None):
         (config, manifest, layers) = self.prepare_new()

      # Pack it all up and store for upload().
      config_bytes = json.dumps(config, indent=2).encode("UTF-8")
      config_hash = ch.bytes_hash(config_bytes)
      manifest["config"]["size"] = len(config_bytes)
      manifest["config"]["digest"] = "sha256:" + config_hash
      ch.DEBUG("config: %s\n%s" % (config_hash, config_bytes.decode("UTF-8")))
      manifest_bytes = json.dumps(manifest, indent=2).encode("UTF-8")
      ch.DEBUG("manifest:\n%s" % manifest_bytes.decode("UTF-8"))

      # Upload cache enabled; store prepared config and manifest. (layers are
      # already stored, see prepare_new()).
      if (upload_cache is not None):
         self.path_manifest.file_write(manifest_bytes)
         self.path_config.file_write(config_bytes)

      self.layers = layers
      self.config = config_bytes
      self.manifest = manifest_bytes

   def prepare_new(self):
      tars_uc = self.image.tarballs_write(ch.storage.upload_cache)
      tars_c = list()
      config = self.config_new()
      manifest = self.manifest_new()
      # Prepare layers.
      for (i, tar_uc) in enumerate(tars_uc, start=1):
         ch.INFO("layer %d/%d: preparing" % (i, len(tars_uc)))
         path_uc = ch.storage.upload_cache // tar_uc
         hash_uc = path_uc.file_hash()
         config["rootfs"]["diff_ids"].append("sha256:" + hash_uc)
         size_uc = path_uc.file_size()
         # gzip changes the file's path by appending a `.gz` to it, which
         # causes its subsequent Filesystem rename method to fail. We use an
         # temp variable, path_c_rename along with os.rename() to get around
         # this.
         path_c_rename = path_uc.file_gzip(["-9", "--no-name"])
         hash_c = path_c_rename.file_hash()
         path_c = ch.storage.upload_cache // str(hash_c + '.tar.gz')
         os.rename(path_c_rename, path_c)
         tar_c = path_c.name
         size_c = path_c.file_size()
         tars_c.append((hash_c, path_c))
         manifest["layers"].append({ "mediaType": rg.TYPE_LAYER,
                                     "size": size_c,
                                     "digest": "sha256:" + hash_c })
      # Prepare metadata.
      ch.INFO("preparing metadata")
      self.image.metadata_load()
      # Environment. Note that this is *not* a dictionary for some reason but
      # a list of name/value pairs separated by equals [1], with no quoting.
      #
      # [1]: https://github.com/opencontainers/image-spec/blob/main/config.md
      config['config']['Env'] = ["%s=%s" % (k, v)
                                 for k, v
                                 in self.image.metadata.get("env", {}).items()]
      # History. Some registries, e.g., Quay, use history metadata for simple
      # sanity checks. For example, when an image’s number of "empty_layer"
      # history entries doesn’t match the number of layers being uploaded,
      # Quay will reject the image upload.
      #
      # This type of error checking is odd as the empty_layer key is optional
      # (https://github.com/opencontainers/image-spec/blob/main/config.md).
      #
      # Thus, to push images built (or pulled) with Charliecloud we ensure the
      # the total number of non-empty layers always totals one (1). To do this
      # we iterate over the history entires backward searching for the first
      # non-empty entry and preserve it; all others are set to empty.
      hist = self.image.metadata["history"]
      non_empty_winner = None
      for i in range(len(hist) - 1, -1, -1):
         if (   "empty_layer" not in hist[i].keys()
             or (    "empty_layer" in hist[i].keys()
                 and not hist[i]["empty_layer"])):
            non_empty_winner = i
            break
      assert(non_empty_winner is not None)
      for i in range(len(hist) - 1):
         if (i != non_empty_winner):
            hist[i]["empty_layer"] = True
      config["history"] = hist
      return (config, manifest, tars_c)

   def push(self):
      self.prepare()
      self.upload()
      self.cleanup()

   def upload(self):
      ch.INFO("starting upload")
      for i in self.layers:
      for (i, (digest, tarball)) in enumerate(self.layers, start=1):
         self.registry.layer_from_file(digest, tarball,
                                 "layer %d/%d: " % (i, len(self.layers)))
      self.registry.config_upload(self.config)
      self.registry.manifest_upload(self.manifest)
      self.registry.close()
