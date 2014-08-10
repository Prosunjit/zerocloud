'''
	sample debug tool.
	if arg for my_debug ( ,arg) is a python dict, it prints as pritty json, otherwise prints normally.

'''


def replace_single_quote_with_double(str):
        import string
        str =  string.replace(str,'\'', '"')
        return str 

def is_json(myjson):
  import json
  try: 
      myjson = replace_single_quote_with_double(myjson)
      json_object = json.loads(myjson)
  except ValueError, e:
      return False
  return True 

def my_debug(str, arg):
        import json
        print "------------------{}----------------------".format(str)

        if type(arg) == dict: 
                #arg = replace_single_quote_with_double(arg)
                print json.dumps(arg, sort_keys=True, indent=4, separators=(',', ': '))
        else:
                print arg 
        print "------------------{}----------------------".format(str)

def test():
        my_debug("dict equivalent json: ", {'a':'a','b':[10,20]})
        my_debug("non dict, non json", [20,30])
        my_debug("string", "string")
if __name__== "__main__":
        test()
